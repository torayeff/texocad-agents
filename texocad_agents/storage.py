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

SOURCE_CONTENT_TYPE = "text/x-python; charset=utf-8"
MODEL_CONTENT_TYPES = {
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".step": "application/step",
    ".stp": "application/step",
    ".stl": "model/stl",
}


@dataclass(frozen=True)
class DesignTables:
    designs: object
    design_revisions: object


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
            partition_key=safe_key(owner_id),
            row_key=safe_key(design_id),
        )
    except ResourceNotFoundError:
        pass

    entity = {
        **azure_table_keys(owner_id, safe_key(design_id)),
        "design_id": design_id,
        "owner_id": owner_id,
        "title": title or design_id,
        **lineage_fields(created_by_thread_id, created_by_run_id),
    }
    designs_table.create_entity(entity)
    return entity


def list_design_revisions(design_revisions_table, owner_id: str, design_id: str):
    revisions = design_revisions_table.query_entities(
        " and ".join([
            f"PartitionKey eq {odata_string(safe_key(owner_id))}",
            f"design_id eq {odata_string(design_id)}",
        ])
    )
    return sorted(revisions, key=lambda revision: revision["revision_number"])


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
    for _ in range(5):
        revision_number = next_design_revision_number(
            design_revisions_table, owner_id, design_id
        )
        entity = {
            **azure_table_keys(
                owner_id,
                f"{safe_key(design_id)}:{revision_number}",
            ),
            "design_id": design_id,
            "owner_id": owner_id,
            "revision_number": revision_number,
            "source_filename": "",
            "source_blob_name": "",
            "source_content_type": "",
            "source_checksum": "",
            "exports_json": "[]",
            **lineage_fields(created_by_thread_id, created_by_run_id),
        }
        try:
            design_revisions_table.create_entity(entity)
            return entity
        except ResourceExistsError:
            continue
    raise RuntimeError("Could not create a unique design revision.")


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


def upload_source_blob(
    storage_container,
    owner_id: str,
    design_id: str,
    revision_number: int,
    filename: str,
    code: str,
) -> StoredBlob:
    blob_name = revision_blob_name(
        owner_id, design_id, revision_number, "source", filename
    )
    data = code.encode("utf-8")
    storage_container.get_blob_client(blob_name).upload_blob(
        data,
        overwrite=False,
        content_settings=ContentSettings(content_type=SOURCE_CONTENT_TYPE),
    )
    return StoredBlob(
        filename=filename,
        blob_name=blob_name,
        content_type=SOURCE_CONTENT_TYPE,
        checksum=hashlib.sha256(data).hexdigest(),
    )


def upload_export_blob(
    storage_container,
    owner_id: str,
    design_id: str,
    revision_number: int,
    model_path: Path,
) -> StoredBlob:
    suffix = model_path.suffix.lower()
    extension = ".step" if suffix == ".stp" else suffix
    filename = f"{safe_key(model_path.stem)}{extension}"
    content_type = MODEL_CONTENT_TYPES.get(extension, "application/octet-stream")
    blob_name = revision_blob_name(
        owner_id,
        design_id,
        revision_number,
        "exports",
        filename,
    )
    content_settings = ContentSettings(
        content_type=content_type,
        content_disposition=f'attachment; filename="{filename}"',
    )

    with model_path.open("rb") as model_file:
        storage_container.get_blob_client(blob_name).upload_blob(
            model_file,
            overwrite=False,
            content_settings=content_settings,
        )

    return StoredBlob(
        filename=filename,
        blob_name=blob_name,
        content_type=content_type,
        checksum=sha256_file(model_path),
    )


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
        f"users/{user_scope_hash(owner_id)}/designs/{safe_key(design_id)}"
        f"/revisions/{revision_number}"
        f"/{safe_key(folder)}/{safe_key(filename)}"
    )


def azure_table_keys(owner_id: str, row_key: str) -> dict[str, str]:
    return {
        "PartitionKey": safe_key(owner_id),
        "RowKey": row_key,
    }


def lineage_fields(
    created_by_thread_id: str | None,
    created_by_run_id: str | None,
) -> dict[str, str]:
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by_thread_id": created_by_thread_id or "",
        "created_by_run_id": created_by_run_id or "",
    }


def safe_key(value: str) -> str:
    value = value.strip("/")
    return "".join("_" if char in "/\\#?\n\r\t" else char for char in value)


def user_scope_hash(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def odata_string(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
