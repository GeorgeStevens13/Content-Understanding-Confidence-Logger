"""
Blob helpers — uses Managed Identity (DefaultAzureCredential) only.

The "move" operation is implemented as server-side copy + delete-source, since
Azure Blob Storage has no native rename/move primitive.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import quote

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient, BlobServiceClient

log = logging.getLogger(__name__)

_credential: DefaultAzureCredential | None = None
_blob_service: BlobServiceClient | None = None


def _get_credential() -> DefaultAzureCredential:
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return _credential


def _blob_service_url() -> str:
    # Standard Functions identity-based connection setting.
    explicit = os.getenv("AzureWebJobsStorage__blobServiceUri")
    if explicit:
        return explicit
    account = os.environ["AzureWebJobsStorage__accountName"]
    return f"https://{account}.blob.core.windows.net"


def get_service() -> BlobServiceClient:
    global _blob_service
    if _blob_service is None:
        _blob_service = BlobServiceClient(
            account_url=_blob_service_url(),
            credential=_get_credential(),
        )
    return _blob_service


@dataclass(frozen=True)
class MoveResult:
    destination_container: str
    destination_blob: str
    destination_url: str


def move_blob(
    *,
    source_container: str,
    source_blob: str,
    destination_container: str,
    destination_blob: str | None = None,
    poll_interval_sec: float = 0.5,
    max_wait_sec: float = 60.0,
) -> MoveResult:
    """Server-side copy then delete the source. Preserves blob path by default."""

    dest_blob_name = destination_blob or source_blob
    service = get_service()
    src_client: BlobClient = service.get_blob_client(source_container, source_blob)
    dst_client: BlobClient = service.get_blob_client(destination_container, dest_blob_name)

    src_url = (
        f"{service.url.rstrip('/')}/"
        f"{quote(source_container)}/{quote(source_blob, safe='/')}"
    )

    log.info("Copying %s -> %s/%s", src_url, destination_container, dest_blob_name)
    dst_client.start_copy_from_url(src_url, requires_sync=False)

    # Poll until copy completes (these are same-account copies → usually instant).
    deadline = time.monotonic() + max_wait_sec
    while True:
        props = dst_client.get_blob_properties()
        status = (props.copy.status or "").lower()
        if status == "success":
            break
        if status in ("failed", "aborted"):
            raise RuntimeError(
                f"Copy failed (status={status}): {props.copy.status_description}"
            )
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"Copy to {destination_container}/{dest_blob_name} did not "
                f"complete in {max_wait_sec}s"
            )
        time.sleep(poll_interval_sec)

    src_client.delete_blob()
    log.info("Deleted source %s/%s after successful copy", source_container, source_blob)

    return MoveResult(
        destination_container=destination_container,
        destination_blob=dest_blob_name,
        destination_url=dst_client.url,
    )


def write_text_blob(
    *,
    container: str,
    blob_name: str,
    text: str,
    content_type: str = "text/plain; charset=utf-8",
) -> None:
    """Used to drop a `.error.txt` next to a failed file."""
    from azure.storage.blob import ContentSettings

    client = get_service().get_blob_client(container, blob_name)
    client.upload_blob(
        text.encode("utf-8"),
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type),
    )
