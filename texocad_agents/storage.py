import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableServiceClient
from azure.storage.blob import BlobServiceClient, ContentSettings

DESIGNS_TABLE_NAME = "texocaddesigns"
DESIGN_REVISIONS_TABLE_NAME = "texocaddesignrevisions"
DESIGN_PREVIEWS_TABLE_NAME = "texocaddesignpreviews"

STEP_CONTENT_TYPE = "application/step"
RENDER_CONTENT_TYPE = "image/png"
MAX_MODEL_BYTES = 100 * 1024 * 1024
REVISION_CONTENTION_RETRIES = 5


@dataclass(frozen=True)
class DesignTables:
    designs: object
    design_revisions: object
    design_previews: object


@dataclass(frozen=True)
class StoredBlob:
    filename: str
    blob_name: str
    content_type: str
    checksum: str


def get_storage_container():
    return BlobServiceClient.from_connection_string(
        os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    ).get_container_client(os.environ["AZURE_STORAGE_CONTAINER_NAME"])


def get_design_tables() -> DesignTables:
    service = TableServiceClient.from_connection_string(
        os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    )
    return DesignTables(
        designs=service.create_table_if_not_exists(DESIGNS_TABLE_NAME),
        design_revisions=service.create_table_if_not_exists(
            DESIGN_REVISIONS_TABLE_NAME
        ),
        design_previews=service.create_table_if_not_exists(DESIGN_PREVIEWS_TABLE_NAME),
    )


def ensure_design(
    designs_table,
    owner_id: str,
    design_id: str,
    *,
    title: str | None,
    created_by_thread_id: str | None,
    created_by_run_id: str | None,
):
    try:
        return designs_table.get_entity(
            partition_key=escape_key(owner_id),
            row_key=escape_key(design_id),
        )
    except ResourceNotFoundError:
        pass

    entity = {
        **table_keys(owner_id, escape_key(design_id)),
        "design_id": design_id,
        "owner_id": owner_id,
        "title": title or design_id,
        **lineage(created_by_thread_id, created_by_run_id),
    }
    try:
        designs_table.create_entity(entity)
        return entity
    except ResourceExistsError:
        return designs_table.get_entity(
            partition_key=escape_key(owner_id),
            row_key=escape_key(design_id),
        )


def list_design_revisions(design_revisions_table, owner_id: str, design_id: str):
    revisions = design_revisions_table.query_entities(
        " and ".join([
            f"PartitionKey eq {odata_quote(escape_key(owner_id))}",
            f"design_id eq {odata_quote(design_id)}",
        ])
    )
    return sorted(revisions, key=lambda revision: revision["revision_number"])


def get_design_revision(
    design_revisions_table,
    owner_id: str,
    design_id: str,
    revision_number: int,
):
    return design_revisions_table.get_entity(
        partition_key=escape_key(owner_id),
        row_key=revision_row_key(design_id, revision_number),
    )


def next_design_revision_number(
    design_revisions_table,
    owner_id: str,
    design_id: str,
) -> int:
    revisions = list_design_revisions(design_revisions_table, owner_id, design_id)
    if not revisions:
        return 1
    return int(revisions[-1]["revision_number"]) + 1


def create_design_revision(
    design_revisions_table,
    owner_id: str,
    design_id: str,
    *,
    created_by_thread_id: str | None,
    created_by_run_id: str | None,
):
    """Allocate a fresh revision row for a design.

    Uses optimistic concurrency: read the current max revision_number, try to
    create the next row, and retry on ResourceExistsError (another writer
    raced us). Gives up after REVISION_CONTENTION_RETRIES attempts.
    """
    for attempt in range(REVISION_CONTENTION_RETRIES):
        revision_number = next_design_revision_number(
            design_revisions_table, owner_id, design_id
        )
        entity = {
            **table_keys(
                owner_id,
                revision_row_key(design_id, revision_number),
            ),
            "design_id": design_id,
            "owner_id": owner_id,
            "revision_number": revision_number,
            "source_filename": "",
            "source_blob_name": "",
            "source_content_type": "",
            "source_checksum": "",
            "exports_json": "[]",
            **lineage(created_by_thread_id, created_by_run_id),
        }
        try:
            design_revisions_table.create_entity(entity)
            return entity
        except ResourceExistsError:
            continue
    raise RuntimeError(
        f"Could not allocate a unique design revision for {design_id!r} "
        f"after {REVISION_CONTENTION_RETRIES} contended attempts."
    )


def save_design_revision_blobs(
    design_revisions_table,
    design_revision: dict,
    source: StoredBlob,
    exports: list[StoredBlob],
):
    entity = dict(design_revision)
    entity.update({
        "source_filename": source.filename,
        "source_blob_name": source.blob_name,
        "source_content_type": source.content_type,
        "source_checksum": source.checksum,
        "exports_json": json.dumps([asdict(export) for export in exports]),
    })
    design_revisions_table.upsert_entity(entity)
    return entity


def upload_blob(
    storage_container,
    blob_name: str,
    data: bytes | Path,
    *,
    filename: str,
    content_type: str,
    content_disposition: str | None = None,
    overwrite: bool = True,
) -> StoredBlob:
    """Upload bytes or a file path as a blob and return its StoredBlob record.

    overwrite defaults to True because blob names are deterministic from
    (owner, design, revision, folder, filename); a retry after a partial
    upload must not be wedged by ResourceExistsError.
    """
    settings = ContentSettings(
        content_type=content_type,
        content_disposition=content_disposition,
    )
    blob_client = storage_container.get_blob_client(blob_name)
    if isinstance(data, Path):
        with data.open("rb") as source:
            blob_client.upload_blob(
                source,
                overwrite=overwrite,
                content_settings=settings,
            )
        checksum = sha256_file(data)
    else:
        blob_client.upload_blob(
            data,
            overwrite=overwrite,
            content_settings=settings,
        )
        checksum = hashlib.sha256(data).hexdigest()
    return StoredBlob(
        filename=filename,
        blob_name=blob_name,
        content_type=content_type,
        checksum=checksum,
    )


def save_design_preview(
    design_previews_table,
    owner_id: str,
    design_id: str,
    revision_number: int,
    view: str,
    preview: StoredBlob,
    *,
    source_export_checksum: str,
    camera: tuple[float, float, float] | None,
    up: tuple[float, float, float] | None,
    created_by_thread_id: str | None,
    created_by_run_id: str | None,
):
    entity = {
        **table_keys(
            owner_id,
            preview_row_key(design_id, revision_number, view),
        ),
        "design_id": design_id,
        "owner_id": owner_id,
        "revision_number": revision_number,
        "view": view,
        "camera_json": json.dumps(camera) if camera else "",
        "up_json": json.dumps(up) if up else "",
        "filename": preview.filename,
        "blob_name": preview.blob_name,
        "content_type": preview.content_type,
        "checksum": preview.checksum,
        "source_export_checksum": source_export_checksum,
        **lineage(created_by_thread_id, created_by_run_id),
    }
    design_previews_table.upsert_entity(entity)
    return entity


def blob_path_from_url(value: str, container_name: str | None = None) -> str:
    parsed = urlparse(value)
    if not parsed.scheme:
        return value.lstrip("/")

    container = container_name or os.getenv("AZURE_STORAGE_CONTAINER_NAME", "")
    parts = [unquote(part) for part in parsed.path.lstrip("/").split("/") if part]
    if container and parts[:1] == [container]:
        parts = parts[1:]
    return "/".join(parts)


def revision_blob_name(
    owner_id: str,
    design_id: str,
    revision_number: int,
    folder: str,
    filename: str,
) -> str:
    return (
        f"users/{scope_hash(owner_id)}/designs/{escape_key(design_id)}"
        f"/revisions/{revision_number}"
        f"/{escape_key(folder)}/{escape_key(filename)}"
    )


def revision_row_key(design_id: str, revision_number: int) -> str:
    return f"{escape_key(design_id)}:{revision_number}"


def preview_row_key(design_id: str, revision_number: int, view: str) -> str:
    return f"{revision_row_key(design_id, revision_number)}:{escape_key(view)}"


def table_keys(owner_id: str, row_key: str) -> dict[str, str]:
    return {
        "PartitionKey": escape_key(owner_id),
        "RowKey": row_key,
    }


def lineage(
    created_by_thread_id: str | None,
    created_by_run_id: str | None,
) -> dict[str, str]:
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by_thread_id": created_by_thread_id or "",
        "created_by_run_id": created_by_run_id or "",
    }


def escape_key(value: str) -> str:
    value = value.strip("/").strip()
    if not value:
        raise ValueError("escape_key received an empty value")
    cleaned = "".join(
        "_" if char in "/\\#?\n\r\t" or ord(char) < 0x20 else char for char in value
    )
    return cleaned[:200]


def scope_hash(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def odata_quote(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
