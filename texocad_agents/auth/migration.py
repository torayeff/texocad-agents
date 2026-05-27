"""Helpers for migrating anonymous design ownership after sign-in.

LangGraph thread ownership must be updated by the application using the
LangGraph API. This module handles the TexoCAD storage side: Azure table rows
and blobs created by tools under the anonymous owner.
"""

import json
from dataclasses import dataclass

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import ContentSettings

from texocad_agents.storage import (
    StoredBlob,
    blob_fields,
    escape_key,
    odata_quote,
    preview_row_key,
    revision_row_key,
    scope_hash,
    table_keys,
)


@dataclass(frozen=True)
class OwnershipMigrationResult:
    """Summary of migrated TexoCAD storage records."""

    design_id: str
    source_owner_id: str
    target_owner_id: str
    blobs_copied: int
    revisions_copied: int
    previews_copied: int


def migrate_design_owner(
    *,
    tables,
    storage_container,
    source_owner_id: str,
    target_owner_id: str,
    design_id: str,
    delete_source: bool = False,
) -> OwnershipMigrationResult:
    """Copy one design from an anonymous owner partition to a signed-in owner.

    Args:
        tables: `DesignTables` from `texocad_agents.storage.get_design_tables()`.
        storage_container: Azure blob container client.
        source_owner_id: Existing anonymous owner, for example `anon:abc`.
        target_owner_id: Signed-in Clerk user ID.
        design_id: Design/thread ID to migrate.
        delete_source: Whether to delete source rows and blobs after successful
            copy.

    Returns:
        Migration summary.

    Raises:
        RuntimeError: If target rows already exist.
        ResourceNotFoundError: If the source design does not exist.
    """

    if source_owner_id == target_owner_id:
        raise ValueError("source_owner_id and target_owner_id must differ")

    design = tables.designs.get_entity(
        partition_key=escape_key(source_owner_id),
        row_key=escape_key(design_id),
    )
    _create_migrated_entity(
        tables.designs,
        design,
        owner_id=target_owner_id,
        row_key=escape_key(design_id),
    )

    blobs_copied = 0
    revisions = _list_design_entities(
        tables.design_revisions,
        source_owner_id,
        design_id,
    )
    for revision in revisions:
        migrated_revision, copied = _migrate_revision_entity(
            storage_container,
            revision,
            source_owner_id=source_owner_id,
            target_owner_id=target_owner_id,
        )
        blobs_copied += copied
        _create_migrated_entity(
            tables.design_revisions,
            migrated_revision,
            owner_id=target_owner_id,
            row_key=revision_row_key(design_id, int(revision["revision_number"])),
        )

    previews = _list_design_entities(tables.design_previews, source_owner_id, design_id)
    for preview in previews:
        migrated_preview, copied = _migrate_preview_entity(
            storage_container,
            preview,
            source_owner_id=source_owner_id,
            target_owner_id=target_owner_id,
        )
        blobs_copied += copied
        _create_migrated_entity(
            tables.design_previews,
            migrated_preview,
            owner_id=target_owner_id,
            row_key=preview_row_key(
                design_id,
                int(preview["revision_number"]),
                str(preview["view"]),
            ),
        )

    if delete_source:
        _delete_source_blobs(
            storage_container,
            source_owner_id=source_owner_id,
            design_id=design_id,
        )
        _delete_source_entities(
            tables,
            source_owner_id=source_owner_id,
            design_id=design_id,
            revisions=revisions,
            previews=previews,
        )

    return OwnershipMigrationResult(
        design_id=design_id,
        source_owner_id=source_owner_id,
        target_owner_id=target_owner_id,
        blobs_copied=blobs_copied,
        revisions_copied=len(revisions),
        previews_copied=len(previews),
    )


def _list_design_entities(table, owner_id: str, design_id: str) -> list[dict]:
    return list(
        table.query_entities(
            " and ".join([
                f"PartitionKey eq {odata_quote(escape_key(owner_id))}",
                f"design_id eq {odata_quote(design_id)}",
            ])
        )
    )


def _create_migrated_entity(
    table, entity: dict, *, owner_id: str, row_key: str
) -> None:
    migrated = dict(entity)
    migrated.update(table_keys(owner_id, row_key))
    migrated["owner_id"] = owner_id
    try:
        table.create_entity(migrated)
    except ResourceExistsError as exc:
        raise RuntimeError(
            f"Target migrated entity already exists: {owner_id}/{row_key}"
        ) from exc


def _migrate_revision_entity(
    storage_container,
    revision: dict,
    *,
    source_owner_id: str,
    target_owner_id: str,
) -> tuple[dict, int]:
    migrated = dict(revision)
    copied = 0

    source_blob_name = str(revision.get("source_blob_name") or "")
    if source_blob_name:
        migrated_source = _copy_blob_to_owner(
            storage_container,
            source_blob_name,
            source_owner_id=source_owner_id,
            target_owner_id=target_owner_id,
            filename=str(revision.get("source_filename") or ""),
            content_type=str(revision.get("source_content_type") or ""),
            checksum=str(revision.get("source_checksum") or ""),
        )
        copied += 1
        migrated.update(blob_fields(migrated_source, prefix="source_"))

    exports = json.loads(str(revision.get("exports_json") or "[]"))
    migrated_exports = []
    for export in exports:
        blob_name = str(export.get("blob_name") or "")
        if not blob_name:
            continue
        migrated_export = _copy_blob_to_owner(
            storage_container,
            blob_name,
            source_owner_id=source_owner_id,
            target_owner_id=target_owner_id,
            filename=str(export.get("filename") or ""),
            content_type=str(export.get("content_type") or ""),
            checksum=str(export.get("checksum") or ""),
        )
        copied += 1
        migrated_exports.append(blob_fields(migrated_export))
    migrated["exports_json"] = json.dumps(migrated_exports)

    return migrated, copied


def _migrate_preview_entity(
    storage_container,
    preview: dict,
    *,
    source_owner_id: str,
    target_owner_id: str,
) -> tuple[dict, int]:
    migrated = dict(preview)
    blob_name = str(preview.get("blob_name") or "")
    if not blob_name:
        return migrated, 0

    migrated_preview = _copy_blob_to_owner(
        storage_container,
        blob_name,
        source_owner_id=source_owner_id,
        target_owner_id=target_owner_id,
        filename=str(preview.get("filename") or ""),
        content_type=str(preview.get("content_type") or ""),
        checksum=str(preview.get("checksum") or ""),
    )
    migrated.update(blob_fields(migrated_preview))
    return migrated, 1


def _copy_blob_to_owner(
    storage_container,
    source_blob_name: str,
    *,
    source_owner_id: str,
    target_owner_id: str,
    filename: str,
    content_type: str,
    checksum: str,
) -> StoredBlob:
    target_blob_name = _rewrite_owner_blob_name(
        source_blob_name,
        source_owner_id=source_owner_id,
        target_owner_id=target_owner_id,
    )
    source_blob = storage_container.get_blob_client(source_blob_name)
    target_blob = storage_container.get_blob_client(target_blob_name)
    properties = source_blob.get_blob_properties()
    content_settings = properties.content_settings
    target_blob.upload_blob(
        source_blob.download_blob().readall(),
        overwrite=False,
        content_settings=ContentSettings(
            content_type=content_settings.content_type,
            content_disposition=content_settings.content_disposition,
            content_encoding=content_settings.content_encoding,
            content_language=content_settings.content_language,
            content_md5=content_settings.content_md5,
            cache_control=content_settings.cache_control,
        ),
    )
    return StoredBlob(
        filename=filename or source_blob_name.rsplit("/", 1)[-1],
        blob_name=target_blob_name,
        content_type=content_type or content_settings.content_type or "",
        checksum=checksum,
    )


def _rewrite_owner_blob_name(
    blob_name: str,
    *,
    source_owner_id: str,
    target_owner_id: str,
) -> str:
    source_prefix = f"users/{scope_hash(source_owner_id)}/"
    if not blob_name.startswith(source_prefix):
        raise ValueError("Source blob is outside the source owner's scope")
    target_prefix = f"users/{scope_hash(target_owner_id)}/"
    return f"{target_prefix}{blob_name[len(source_prefix) :]}"


def _delete_source_entities(
    tables,
    *,
    source_owner_id: str,
    design_id: str,
    revisions: list[dict],
    previews: list[dict],
) -> None:
    partition_key = escape_key(source_owner_id)
    for preview in previews:
        tables.design_previews.delete_entity(
            partition_key=partition_key,
            row_key=str(preview["RowKey"]),
        )
    for revision in revisions:
        tables.design_revisions.delete_entity(
            partition_key=partition_key,
            row_key=str(revision["RowKey"]),
        )
    tables.designs.delete_entity(
        partition_key=partition_key,
        row_key=escape_key(design_id),
    )


def _delete_source_blobs(
    storage_container,
    *,
    source_owner_id: str,
    design_id: str,
) -> None:
    prefix = f"users/{scope_hash(source_owner_id)}/designs/{escape_key(design_id)}/"
    for blob in storage_container.list_blobs(name_starts_with=prefix):
        storage_container.delete_blob(blob.name)
