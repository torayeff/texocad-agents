import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableServiceClient
from azure.storage.blob import BlobServiceClient, ContentSettings

DESIGNS_TABLE_NAME = "designs"
DESIGN_REVISIONS_TABLE_NAME = "designrevisions"
ARTIFACT_REVISIONS_TABLE_NAME = "artifactrevisions"

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
    artifact_revisions: object


@dataclass(frozen=True)
class StoredArtifact:
    artifact_revision_id: str
    artifact_type: str
    revision_number: int
    blob_name: str
    content_type: str
    checksum: str


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def safe_key(value: str) -> str:
    value = value.strip("/")
    return "".join("_" if char in "/\\#?\n\r\t" else char for char in value)


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
        artifact_revisions=service.create_table_if_not_exists(
            ARTIFACT_REVISIONS_TABLE_NAME
        ),
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


def odata_string(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def model_blob_extension(model_path: Path) -> str:
    suffix = model_path.suffix.lower()
    return ".step" if suffix == ".stp" else suffix


def source_blob_name(
    owner_id: str,
    design_id: str,
    design_revision_id: str,
    artifact_revision_id: str,
) -> str:
    return (
        f"designs/{safe_key(owner_id)}/{safe_key(design_id)}"
        f"/revisions/{safe_key(design_revision_id)}"
        f"/source/{safe_key(artifact_revision_id)}.py"
    )


def export_blob_name(
    owner_id: str,
    design_id: str,
    design_revision_id: str,
    artifact_revision_id: str,
    extension: str,
) -> str:
    return (
        f"designs/{safe_key(owner_id)}/{safe_key(design_id)}"
        f"/revisions/{safe_key(design_revision_id)}"
        f"/exports/{safe_key(artifact_revision_id)}{extension}"
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_design(designs_table, owner_id: str, design_id: str):
    try:
        return designs_table.get_entity(
            partition_key=safe_key(owner_id),
            row_key=safe_key(design_id),
        )
    except ResourceNotFoundError:
        return None


def ensure_design(
    designs_table,
    owner_id: str,
    design_id: str,
    *,
    title: str | None,
    created_by_thread_id: str | None,
    created_by_run_id: str | None,
):
    existing = get_design(designs_table, owner_id, design_id)
    if existing is not None:
        return existing

    entity = {
        "PartitionKey": safe_key(owner_id),
        "RowKey": safe_key(design_id),
        "design_id": design_id,
        "owner_id": owner_id,
        "title": title or design_id,
        "created_at": now_iso(),
        "created_by_thread_id": created_by_thread_id or "",
        "created_by_run_id": created_by_run_id or "",
    }
    designs_table.create_entity(entity)
    return entity


def list_designs(designs_table, owner_id: str):
    return list(
        designs_table.query_entities(
            f"PartitionKey eq {odata_string(safe_key(owner_id))}"
        )
    )


def list_design_revisions(design_revisions_table, owner_id: str, design_id: str):
    revisions = design_revisions_table.query_entities(
        " and ".join(
            [
                f"PartitionKey eq {odata_string(safe_key(owner_id))}",
                f"design_id eq {odata_string(design_id)}",
            ]
        )
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
    revision_name: str | None,
    created_by_thread_id: str | None,
    created_by_run_id: str | None,
):
    revision_number = next_design_revision_number(
        design_revisions_table,
        owner_id,
        design_id,
    )
    design_revision_id = new_id("drev")
    entity = {
        "PartitionKey": safe_key(owner_id),
        "RowKey": f"{safe_key(design_id)}:{safe_key(design_revision_id)}",
        "design_revision_id": design_revision_id,
        "design_id": design_id,
        "owner_id": owner_id,
        "revision_number": revision_number,
        "revision_name": revision_name or f"r{revision_number}",
        "created_at": now_iso(),
        "created_by_thread_id": created_by_thread_id or "",
        "created_by_run_id": created_by_run_id or "",
    }
    design_revisions_table.create_entity(entity)
    return entity


def create_artifact_revision(
    artifact_revisions_table,
    owner_id: str,
    design_id: str,
    design_revision_id: str,
    artifact: StoredArtifact,
    *,
    filename: str,
    created_by_thread_id: str | None,
    created_by_run_id: str | None,
):
    entity = {
        "PartitionKey": safe_key(owner_id),
        "RowKey": (
            f"{safe_key(design_id)}:{safe_key(design_revision_id)}:"
            f"{safe_key(artifact.artifact_revision_id)}"
        ),
        "artifact_revision_id": artifact.artifact_revision_id,
        "artifact_type": artifact.artifact_type,
        "design_id": design_id,
        "design_revision_id": design_revision_id,
        "owner_id": owner_id,
        "revision_number": artifact.revision_number,
        "filename": filename,
        "blob_name": artifact.blob_name,
        "content_type": artifact.content_type,
        "checksum": artifact.checksum,
        "created_at": now_iso(),
        "created_by_thread_id": created_by_thread_id or "",
        "created_by_run_id": created_by_run_id or "",
    }
    artifact_revisions_table.create_entity(entity)
    return entity


def upload_source_artifact(
    storage_container,
    owner_id: str,
    design_id: str,
    design_revision_id: str,
    revision_number: int,
    code: str,
) -> StoredArtifact:
    artifact_revision_id = new_id("src")
    blob_name = source_blob_name(
        owner_id,
        design_id,
        design_revision_id,
        artifact_revision_id,
    )
    data = code.encode("utf-8")
    storage_container.get_blob_client(blob_name).upload_blob(
        data,
        overwrite=False,
        content_settings=ContentSettings(content_type=SOURCE_CONTENT_TYPE),
    )
    return StoredArtifact(
        artifact_revision_id=artifact_revision_id,
        artifact_type="source",
        revision_number=revision_number,
        blob_name=blob_name,
        content_type=SOURCE_CONTENT_TYPE,
        checksum=sha256_bytes(data),
    )


def upload_export_artifact(
    storage_container,
    owner_id: str,
    design_id: str,
    design_revision_id: str,
    revision_number: int,
    model_path: Path,
) -> StoredArtifact:
    artifact_revision_id = new_id("exp")
    extension = model_blob_extension(model_path)
    content_type = MODEL_CONTENT_TYPES.get(extension, "application/octet-stream")
    blob_name = export_blob_name(
        owner_id,
        design_id,
        design_revision_id,
        artifact_revision_id,
        extension,
    )
    content_settings = ContentSettings(
        content_type=content_type,
        content_disposition=f'attachment; filename="{artifact_revision_id}{extension}"',
    )

    with model_path.open("rb") as model_file:
        storage_container.get_blob_client(blob_name).upload_blob(
            model_file,
            overwrite=False,
            content_settings=content_settings,
        )

    return StoredArtifact(
        artifact_revision_id=artifact_revision_id,
        artifact_type="export",
        revision_number=revision_number,
        blob_name=blob_name,
        content_type=content_type,
        checksum=sha256_file(model_path),
    )
