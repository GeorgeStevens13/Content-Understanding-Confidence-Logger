"""
Blob helpers — uses Managed Identity (DefaultAzureCredential) only.

The "move" operation is implemented as server-side copy + delete-source, since
Azure Blob Storage has no native rename/move primitive.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import quote

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient, BlobLeaseClient, BlobServiceClient

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
    source_lease_id: str | None = None,
    poll_interval_sec: float = 0.5,
    max_wait_sec: float = 60.0,
) -> MoveResult:
    """Server-side copy then delete the source. Preserves blob path by default.

    If the source blob is currently leased by this worker, pass the lease id as
    `source_lease_id` so the final `delete_blob` call is authorised.
    """

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

    if source_lease_id:
        src_client.delete_blob(lease=source_lease_id)
    else:
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


# ---------------------------------------------------------------------------
# Batch-mode helpers (used by the timer-triggered ingestion loop).
# ---------------------------------------------------------------------------

def list_blob_names(
    *,
    container: str,
    name_starts_with: str | None = None,
    max_results: int | None = None,
) -> list[str]:
    """Return blob names in `container`, optionally filtered by prefix and capped.

    Uses server-side paging via `list_blobs` and stops as soon as `max_results`
    names have been collected so we don't enumerate huge containers needlessly.
    """
    container_client = get_service().get_container_client(container)
    names: list[str] = []
    iterator = container_client.list_blobs(name_starts_with=name_starts_with)
    for blob in iterator:
        names.append(blob.name)
        if max_results is not None and len(names) >= max_results:
            break
    return names


def read_blob_bytes(*, container: str, blob_name: str) -> bytes:
    """Download a blob's contents as bytes."""
    client = get_service().get_blob_client(container, blob_name)
    return client.download_blob().readall()


@contextmanager
def acquire_short_lease(
    *,
    container: str,
    blob_name: str,
    lease_duration_sec: int = 60,
) -> Iterator[BlobLeaseClient | None]:
    """Best-effort exclusive lease on a blob.

    Yields the `BlobLeaseClient` on success, or `None` if the lease could not be
    acquired (already leased by another worker, blob missing, etc.). Always
    releases the lease on exit so a failed run doesn't keep blobs locked for
    the full `lease_duration_sec`.

    A 60-second lease is more than enough for one CU JSON ingest and short
    enough that a crashed worker won't block the next 15-minute tick.
    """
    client = get_service().get_blob_client(container, blob_name)
    lease: BlobLeaseClient | None = None
    try:
        lease = client.acquire_lease(lease_duration=lease_duration_sec)
    except ResourceNotFoundError:
        log.info("Blob %s/%s vanished before lease could be taken", container, blob_name)
        yield None
        return
    except HttpResponseError as exc:
        # 409 LeaseAlreadyPresent => another worker is processing this blob.
        if getattr(exc, "status_code", None) == 409:
            log.info("Blob %s/%s already leased; skipping this tick", container, blob_name)
            yield None
            return
        raise

    try:
        yield lease
    finally:
        try:
            lease.release()
        except Exception:  # pragma: no cover — lease may already be broken/expired
            log.debug("Lease release failed for %s/%s (ignored)", container, blob_name)
