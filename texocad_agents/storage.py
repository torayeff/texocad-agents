"""Persist TexoCAD designs, revisions, previews, and blobs in Azure storage."""

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableServiceClient
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContentSettings,
    generate_blob_sas,
)

DESIGNS_TABLE_NAME = "texocaddesigns"
DESIGN_REVISIONS_TABLE_NAME = "texocaddesignrevisions"
DESIGN_PREVIEWS_TABLE_NAME = "texocaddesignpreviews"

STEP_CONTENT_TYPE = "application/step"
GLB_CONTENT_TYPE = "model/gltf-binary"
RENDER_CONTENT_TYPE = "image/png"
MAX_MODEL_BYTES = 250 * 1024 * 1024
REVISION_CONTENTION_RETRIES = 5


@dataclass(frozen=True)
class DesignTables:
    """Azure table clients for design metadata.

    Attributes:
        designs: Table client for design rows.
        design_revisions: Table client for revision rows.
        design_previews: Table client for preview rows.
    """

    designs: object
    design_revisions: object
    design_previews: object


@dataclass(frozen=True)
class StoredBlob:
    """Metadata for a blob stored as part of a design artifact.

    Attributes:
        filename: User-facing filename.
        blob_name: Container-relative Azure blob path.
        content_type: MIME type assigned to the blob.
        checksum: SHA-256 checksum of the uploaded content.
    """

    filename: str
    blob_name: str
    content_type: str
    checksum: str


def get_storage_container():
    """Return the configured Azure Blob container client.

    Returns:
        Azure container client for `AZURE_STORAGE_CONTAINER_NAME`.
    """

    return BlobServiceClient.from_connection_string(
        os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    ).get_container_client(os.environ["AZURE_STORAGE_CONTAINER_NAME"])


def get_design_tables() -> DesignTables:
    """Create or return Azure tables used for design metadata.

    Returns:
        Table clients for designs, revisions, and previews.
    """

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
    """Create a design row if it does not already exist.

    Args:
        designs_table: Azure Table client for design rows.
        owner_id: Authenticated owner identity.
        design_id: Stable design identifier.
        title: Optional human-readable title for newly created designs.
        created_by_thread_id: LangGraph thread that created the design.
        created_by_run_id: Optional LangGraph run that created the design.

    Returns:
        Existing or newly created design entity.
    """

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
    """List all revisions for a design in ascending revision order.

    Args:
        design_revisions_table: Azure Table client for revision rows.
        owner_id: Authenticated owner identity.
        design_id: Stable design identifier.

    Returns:
        List of revision entities sorted by `revision_number`.
    """

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
    """Fetch one design revision entity.

    Args:
        design_revisions_table: Azure Table client for revision rows.
        owner_id: Authenticated owner identity.
        design_id: Stable design identifier.
        revision_number: Revision number to fetch.

    Returns:
        Matching design revision entity.
    """

    return design_revisions_table.get_entity(
        partition_key=escape_key(owner_id),
        row_key=revision_row_key(design_id, revision_number),
    )


def next_design_revision_number(
    design_revisions_table,
    owner_id: str,
    design_id: str,
) -> int:
    """Compute the next revision number for a design.

    Args:
        design_revisions_table: Azure Table client for revision rows.
        owner_id: Authenticated owner identity.
        design_id: Stable design identifier.

    Returns:
        One greater than the latest stored revision number, or 1 for a new
        design.
    """

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

    Args:
        design_revisions_table: Azure Table client for revision rows.
        owner_id: Authenticated owner identity.
        design_id: Stable design identifier.
        created_by_thread_id: LangGraph thread that created the revision.
        created_by_run_id: Optional LangGraph run that created the revision.

    Returns:
        Newly created design revision entity.

    Raises:
        RuntimeError: If a unique revision cannot be allocated after retries.
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
    """Persist source and export blob metadata on a revision row.

    Args:
        design_revisions_table: Azure Table client for revision rows.
        design_revision: Existing revision entity to update.
        source: Stored source artifact metadata.
        exports: Stored export artifact metadata.

    Returns:
        Updated revision entity.
    """

    entity = dict(design_revision)
    entity.update({
        **blob_fields(source, prefix="source_"),
        "exports_json": json.dumps([blob_fields(export) for export in exports]),
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

    Args:
        storage_container: Azure Blob container client.
        blob_name: Container-relative destination blob name.
        data: Bytes or local file path to upload.
        filename: User-facing filename for the stored blob.
        content_type: MIME type assigned to the blob.
        content_disposition: Optional Content-Disposition header.
        overwrite: Whether an existing blob may be replaced.

    Returns:
        Stored blob metadata including checksum.
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


def staging_blob_name(owner_id: str, design_id: str, filename: str) -> str:
    """Build a unique staging blob path for an artifact produced remotely.

    Staged blobs are uploaded by trusted remote workers (such as Modal
    containers) before a revision exists, then promoted to their final
    revision path with `promote_staged_blob`.

    Args:
        owner_id: Authenticated owner identity.
        design_id: Stable design identifier.
        filename: Artifact filename.

    Returns:
        Container-relative blob path under the owner's staging area.
    """

    return (
        f"users/{scope_hash(owner_id)}/designs/{escape_key(design_id)}"
        f"/staging/{uuid.uuid4().hex[:12]}/{escape_key(filename)}"
    )


def generate_blob_upload_url(
    storage_container,
    blob_name: str,
    *,
    ttl: timedelta = timedelta(hours=2),
) -> str:
    """Build a SAS URL granting read, create, and write on a single blob.

    Read permission is included so the URL can also serve as the source of a
    server-side copy when the staged blob is promoted.

    Args:
        storage_container: Azure Blob container client.
        blob_name: Container-relative blob path the SAS is scoped to.
        ttl: How long the SAS stays valid.

    Returns:
        Full blob URL with the SAS query string appended.
    """

    blob_client = storage_container.get_blob_client(blob_name)
    now = datetime.now(timezone.utc)
    sas = generate_blob_sas(
        account_name=blob_client.account_name,
        container_name=storage_container.container_name,
        blob_name=blob_name,
        account_key=storage_container.credential.account_key,
        permission=BlobSasPermissions(read=True, create=True, write=True),
        start=now - timedelta(minutes=15),
        expiry=now + ttl,
    )
    return f"{blob_client.url}?{sas}"


def promote_staged_blob(
    storage_container,
    *,
    staging_sas_url: str,
    staging_name: str,
    blob_name: str,
    filename: str,
    content_type: str,
    checksum: str,
) -> StoredBlob:
    """Server-side copy a staged blob to its final path and drop the stage.

    Uses Put Blob From URL, so the data never leaves Azure. Content settings
    set on the staged blob are carried over to the destination.

    Args:
        storage_container: Azure Blob container client.
        staging_sas_url: SAS URL of the staged blob with read permission.
        staging_name: Container-relative path of the staged blob.
        blob_name: Final container-relative destination blob path.
        filename: User-facing filename for the stored blob.
        content_type: MIME type assigned to the blob.
        checksum: SHA-256 checksum reported by the uploader.

    Returns:
        Stored blob metadata for the promoted blob.
    """

    storage_container.get_blob_client(blob_name).upload_blob_from_url(
        staging_sas_url, overwrite=True
    )
    delete_blob_silently(storage_container, staging_name)
    return StoredBlob(
        filename=filename,
        blob_name=blob_name,
        content_type=content_type,
        checksum=checksum,
    )


def delete_blob_silently(storage_container, blob_name: str) -> None:
    """Delete a blob, ignoring missing blobs and transient failures.

    Args:
        storage_container: Azure Blob container client.
        blob_name: Container-relative blob path to delete.
    """

    try:
        storage_container.delete_blob(blob_name)
    except Exception:
        pass


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
    """Upsert preview image metadata for a design revision.

    Args:
        design_previews_table: Azure Table client for preview rows.
        owner_id: Authenticated owner identity.
        design_id: Stable design identifier.
        revision_number: Revision number the preview belongs to.
        view: Preview view name, such as `iso_ortho`.
        preview: Stored preview image metadata.
        source_export_checksum: Checksum of the export used to render preview.
        camera: Optional camera vector for custom renders.
        up: Optional camera up vector for custom renders.
        created_by_thread_id: LangGraph thread that created the preview.
        created_by_run_id: Optional LangGraph run that created the preview.

    Returns:
        Upserted preview entity.
    """

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
        **blob_fields(preview),
        "source_export_checksum": source_export_checksum,
        **lineage(created_by_thread_id, created_by_run_id),
    }
    design_previews_table.upsert_entity(entity)
    return entity


def blob_path_from_url(value: str, container_name: str | None = None) -> str:
    """Normalize a blob URL or path into a container-relative blob path.

    Args:
        value: Azure blob URL or raw blob path.
        container_name: Optional container name to strip from URL paths.

    Returns:
        Container-relative blob path with URL escaping decoded.
    """

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
    """Build a deterministic blob path for a revision artifact.

    Args:
        owner_id: Authenticated owner identity.
        design_id: Stable design identifier.
        revision_number: Revision number the artifact belongs to.
        folder: Artifact folder name, such as `source` or `exports`.
        filename: Artifact filename.

    Returns:
        Container-relative blob path.
    """

    return (
        f"users/{scope_hash(owner_id)}/designs/{escape_key(design_id)}"
        f"/revisions/{revision_number}"
        f"/{escape_key(folder)}/{escape_key(filename)}"
    )


def revision_row_key(design_id: str, revision_number: int) -> str:
    """Build the Azure Table row key for a design revision.

    Args:
        design_id: Stable design identifier.
        revision_number: Revision number.

    Returns:
        Escaped revision row key.
    """

    return f"{escape_key(design_id)}:{revision_number}"


def preview_row_key(design_id: str, revision_number: int, view: str) -> str:
    """Build the Azure Table row key for a revision preview.

    Args:
        design_id: Stable design identifier.
        revision_number: Revision number.
        view: Preview view name.

    Returns:
        Escaped preview row key.
    """

    return f"{revision_row_key(design_id, revision_number)}:{escape_key(view)}"


def table_keys(owner_id: str, row_key: str) -> dict[str, str]:
    """Return Azure Table partition and row key fields.

    Args:
        owner_id: Authenticated owner identity used as the partition key.
        row_key: Already escaped row key.

    Returns:
        Mapping containing `PartitionKey` and `RowKey`.
    """

    return {
        "PartitionKey": escape_key(owner_id),
        "RowKey": row_key,
    }


def lineage(
    created_by_thread_id: str | None,
    created_by_run_id: str | None,
) -> dict[str, str]:
    """Return standard creation timestamp and LangGraph lineage fields.

    Args:
        created_by_thread_id: LangGraph thread that created the entity.
        created_by_run_id: Optional LangGraph run that created the entity.

    Returns:
        Mapping of lineage fields for Azure Table entities.
    """

    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by_thread_id": created_by_thread_id or "",
        "created_by_run_id": created_by_run_id or "",
    }


def blob_fields(blob: StoredBlob, prefix: str = "") -> dict[str, str]:
    """Convert stored blob metadata into Azure Table fields.

    Args:
        blob: Stored blob metadata.
        prefix: Optional field-name prefix, such as `source_`.

    Returns:
        Mapping of filename, blob name, content type, and checksum fields.
    """

    return {
        f"{prefix}filename": blob.filename,
        f"{prefix}blob_name": blob.blob_name,
        f"{prefix}content_type": blob.content_type,
        f"{prefix}checksum": blob.checksum,
    }


def blob_payload(blob: StoredBlob) -> dict[str, str]:
    """Convert stored blob metadata into API artifact payload fields.

    Args:
        blob: Stored blob metadata.

    Returns:
        JSON-serializable blob payload with camelCase field names.
    """

    return {
        "filename": blob.filename,
        "blobName": blob.blob_name,
        "contentType": blob.content_type,
        "checksum": blob.checksum,
    }


def escape_key(value: str) -> str:
    """Sanitize a value for Azure Table keys and blob path segments.

    Args:
        value: Raw key value to clean.

    Returns:
        Sanitized value truncated to 200 characters.

    Raises:
        ValueError: If `value` is empty after trimming slashes and whitespace.
    """

    value = value.strip("/").strip()
    if not value:
        raise ValueError("escape_key received an empty value")
    cleaned = "".join(
        "_" if char in "/\\#?\n\r\t" or ord(char) < 0x20 else char for char in value
    )
    return cleaned[:200]


def scope_hash(user_id: str) -> str:
    """Hash an owner id for use in scoped blob paths.

    Args:
        user_id: Authenticated owner identity.

    Returns:
        SHA-256 hex digest for the owner id.
    """

    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def odata_quote(value: str) -> str:
    """Quote and escape a string for Azure Table OData filters.

    Args:
        value: String value to quote.

    Returns:
        Single-quoted OData literal.
    """

    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def sha256_file(path: Path) -> str:
    """Stream a file and return its SHA-256 hex digest.

    Args:
        path: Local file path to hash.

    Returns:
        SHA-256 hex digest for the file contents.
    """

    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
