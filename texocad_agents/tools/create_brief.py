"""Publish markdown requirement briefs, with embedded images, as revisions."""

import io
from pathlib import PurePosixPath

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command
from PIL import Image, UnidentifiedImageError

from texocad_agents.storage import (
    get_storage_container,
    revision_blob_name,
    save_design_revision_blobs,
    upload_blob,
)
from texocad_agents.tools._runtime import (
    ToolContext,
    authorize_blob_access,
    create_revision,
    error_message,
    get_tool_context,
    read_runtime_file,
    revision_artifact,
)

BRIEF_CONTENT_TYPE = "text/markdown; charset=utf-8"
BRIEF_EXTENSION = ".md"
MAX_BRIEF_IMAGES = 10
MAX_IMAGE_BYTES = 8 * 1024 * 1024
SUPPORTED_IMAGE_FORMATS: dict[str, str] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
}

BriefImage = tuple[str, bytes, str]


@tool(parse_docstring=True)
def create_brief(
    file_path: str,
    runtime: ToolRuntime,
    images: list[str] | None = None,
) -> Command:
    """Publish a finished markdown brief as a downloadable revision.

    Use this after the requirements brief in the agent filesystem is complete
    and confirmed by the user. The tool stores the markdown together with the
    listed attachment images and creates a new revision the user can download
    and send to a developer, designer, or freelancer. Bundled images keep
    their filenames, so markdown references like `![ref](photo.png)` resolve
    next to the brief. Each LangGraph thread maps to exactly one brief; every
    publish in the same thread creates a new revision of that brief.

    Without a signed-in session the brief cannot be stored; the tool then
    returns the markdown so it can be presented directly in chat.

    Args:
        file_path: Path to the markdown brief in the agent filesystem, for
            example "/briefs/portfolio-website.md".
        runtime: LangGraph tool runtime for the current tool call.
        images: Blob paths or URLs of user-uploaded images embedded in the
            brief. Each must be a PNG, JPEG, or WEBP attachment.
    """

    markdown = read_runtime_file(file_path, runtime)
    if markdown is None:
        return error_message(
            runtime,
            f"Could not find {file_path!r} in the agent filesystem.",
        )
    if not markdown.strip():
        return error_message(
            runtime,
            f"Refused: {file_path!r} is empty; write the brief before publishing.",
        )

    filename = PurePosixPath(file_path).name
    if not filename.lower().endswith(BRIEF_EXTENSION):
        return error_message(
            runtime,
            f"Refused: brief filename {filename!r} must end with '{BRIEF_EXTENSION}'.",
        )
    if any(char in filename for char in ('"', "\\", "\r", "\n")):
        return error_message(
            runtime,
            f"Refused: brief filename {filename!r} must not include quotes, "
            "backslashes, or newlines.",
        )
    if images and len(images) > MAX_BRIEF_IMAGES:
        return error_message(
            runtime,
            f"Too many images ({len(images)}). Maximum is {MAX_BRIEF_IMAGES}.",
        )

    ctx = get_tool_context(runtime)
    if ctx is None:
        return _unstored_brief_message(runtime, markdown=markdown, images=images)

    brief_images, image_error = _collect_brief_images(ctx, images)
    if image_error:
        return error_message(runtime, image_error)

    unreferenced = [name for name, _, _ in brief_images if name not in markdown]

    try:
        tables, storage_container, design_revision, revision_number = create_revision(
            ctx, title=ctx.metadata.get("title") or PurePosixPath(file_path).stem
        )
        source_blob = upload_blob(
            storage_container,
            revision_blob_name(
                ctx.owner_id, ctx.design_id, revision_number, "source", filename
            ),
            markdown.encode("utf-8"),
            filename=filename,
            content_type=BRIEF_CONTENT_TYPE,
        )
        export_blobs = [
            upload_blob(
                storage_container,
                revision_blob_name(
                    ctx.owner_id, ctx.design_id, revision_number, "exports", filename
                ),
                markdown.encode("utf-8"),
                filename=filename,
                content_type=BRIEF_CONTENT_TYPE,
                content_disposition=f'attachment; filename="{filename}"',
            )
        ]
        for image_name, image_bytes, content_type in brief_images:
            export_blobs.append(
                upload_blob(
                    storage_container,
                    revision_blob_name(
                        ctx.owner_id,
                        ctx.design_id,
                        revision_number,
                        "exports",
                        image_name,
                    ),
                    image_bytes,
                    filename=image_name,
                    content_type=content_type,
                    content_disposition=f'inline; filename="{image_name}"',
                )
            )
        design_revision = save_design_revision_blobs(
            tables.design_revisions, design_revision, source_blob, export_blobs
        )
    except Exception as exc:
        return error_message(
            runtime,
            "Failed to persist brief revision.\n"
            f"design_id: {ctx.design_id}\n"
            f"error: {exc}",
        )

    bundled = ", ".join(name for name, _, _ in brief_images) or "none"
    warning = (
        "\nwarning: bundled image(s) not referenced in the brief markdown: "
        + ", ".join(unreferenced)
        if unreferenced
        else ""
    )
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=(
                        "Published brief revision.\n"
                        f"design_id: {ctx.design_id}\n"
                        f"revision_number: {revision_number}\n"
                        f"brief_blob: {export_blobs[0].blob_name}\n"
                        f"bundled_images: {bundled}"
                        f"{warning}"
                    ),
                    tool_call_id=runtime.tool_call_id,
                    additional_kwargs={
                        "texocad_artifact": revision_artifact(
                            artifact_type="texocad.brief_revision",
                            design_id=ctx.design_id,
                            design_revision=design_revision,
                            source_blob=source_blob,
                            exports=export_blobs,
                            renders=[],
                            thread_id=ctx.thread_id,
                            run_id=ctx.run_id,
                        )
                    },
                )
            ]
        }
    )


def _unstored_brief_message(
    runtime: ToolRuntime,
    *,
    markdown: str,
    images: list[str] | None,
) -> Command:
    """Build the fallback response when no signed-in session exists.

    Args:
        runtime: LangGraph tool runtime for the current tool call.
        markdown: Final brief markdown content.
        images: Image paths requested for bundling, if any.

    Returns:
        Command with a tool message carrying the brief inline instead of a
        stored revision.
    """

    image_note = (
        "Images were not bundled; keep their markdown references and tell the "
        "user to send the image files alongside the brief.\n"
        if images
        else ""
    )
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=(
                        "Brief not stored: this session is not signed in, so "
                        "downloadable revisions are unavailable. Do not retry. "
                        "Present the following markdown to the user as the "
                        "final brief and mention that signing in enables "
                        f"downloadable revisions.\n{image_note}\n"
                        f"{markdown}"
                    ),
                    tool_call_id=runtime.tool_call_id,
                )
            ]
        }
    )


def _collect_brief_images(
    ctx: ToolContext,
    images: list[str] | None,
) -> tuple[list[BriefImage], str | None]:
    """Download and validate attachment images referenced by a brief.

    Args:
        ctx: Authenticated tool context used for blob authorization.
        images: Blob paths or URLs of images to bundle with the brief.

    Returns:
        Tuple of `(brief_images, error)`, where each brief image is
        `(filename, bytes, content_type)`. Duplicate paths are dropped;
        distinct blobs sharing a filename are rejected.
    """

    if not images:
        return [], None

    storage_container = get_storage_container()
    brief_images: list[BriefImage] = []
    seen_blobs: set[str] = set()
    seen_names: set[str] = set()

    for image_path in images:
        blob_name = authorize_blob_access(image_path, ctx)
        if blob_name is None:
            return [], f"Refused image {image_path!r}: not in this user's scope."
        if blob_name in seen_blobs:
            continue
        seen_blobs.add(blob_name)

        image_name = PurePosixPath(blob_name).name
        if image_name.lower() in seen_names:
            return [], (
                f"Duplicate image filename {image_name!r}; bundled images "
                "must have distinct filenames."
            )
        seen_names.add(image_name.lower())

        try:
            image_bytes = (
                storage_container.get_blob_client(blob_name).download_blob().readall()
            )
        except Exception as exc:
            return [], f"Could not download image {image_path!r}: {exc}"

        if len(image_bytes) > MAX_IMAGE_BYTES:
            return [], (
                f"Refused image {image_name!r}: {len(image_bytes)} bytes exceeds "
                f"the {MAX_IMAGE_BYTES} byte limit."
            )

        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                image.verify()
                image_format = (image.format or "").upper()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            return [], f"Refused image {image_name!r}: not a readable image ({exc})."

        content_type = SUPPORTED_IMAGE_FORMATS.get(image_format)
        if content_type is None:
            return [], (
                f"Refused image {image_name!r}: unsupported format "
                f"{image_format or 'unknown'!r}; supported formats are "
                "PNG, JPEG, and WEBP."
            )

        brief_images.append((image_name, image_bytes, content_type))

    return brief_images, None
