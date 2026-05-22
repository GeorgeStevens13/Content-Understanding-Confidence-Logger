"""
Azure Function entry point.

Trigger: a timer that fires on `INGEST_SCHEDULE` (default `0 */15 * * * *`,
i.e. every 15 minutes). On each tick we batch-process up to
`BATCH_MAX_FILES` blobs found under the convention

    <SOURCE_CONTAINER>/<usecase>/<analyzer>/<file>.json

For each matching blob:

  1. Acquire a short-lived lease so two ticks (or a tick + a still-writing
     uploader) can't race on the same file.
  2. Read + parse the JSON.
  3. Flatten every leaf field into rows and INSERT into Azure SQL.
  4. MOVE the file to <PROCESSED_CONTAINER>/<usecase>/<analyzer>/<file>.json.
  5. On any failure: MOVE to <FAILED_CONTAINER>/<usecase>/<analyzer>/<file>.json
     and write a sibling `<file>.json.error.txt` with the traceback,
     then record the failure in cu.IngestionErrors.

The loop respects a soft time budget (`BATCH_TIME_BUDGET_SEC`) so we never
bump into the host's `functionTimeout`. Anything not processed this tick is
picked up on the next.

Auth: Managed Identity end-to-end (storage + SQL).
"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
from dataclasses import dataclass

import azure.functions as func

import ingestion
import sql_client
import storage_client

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SOURCE_CONTAINER     = os.getenv("SOURCE_CONTAINER",    "source")
PROCESSED_CONTAINER  = os.getenv("PROCESSED_CONTAINER", "processed")
FAILED_CONTAINER     = os.getenv("FAILED_CONTAINER",    "failed")

# NCRONTAB: seconds minutes hours day month weekday.
# Default: top of the hour and every 15 minutes thereafter (UTC).
INGEST_SCHEDULE      = os.getenv("INGEST_SCHEDULE",     "0 */15 * * * *")

# Safety caps so a single tick stays well inside the function timeout.
BATCH_MAX_FILES      = int(os.getenv("BATCH_MAX_FILES",       "50"))
BATCH_TIME_BUDGET_SEC = float(os.getenv("BATCH_TIME_BUDGET_SEC", "540"))  # 9 minutes

app = func.FunctionApp()


@app.timer_trigger(
    arg_name="timer",
    schedule=INGEST_SCHEDULE,
    run_on_startup=False,
    use_monitor=True,
)
def ingest_content_understanding_batch(timer: func.TimerRequest) -> None:
    """Scan the source container and process up to BATCH_MAX_FILES blobs."""

    if timer.past_due:
        log.warning("Timer is past due — previous invocation may have overrun.")

    started = time.monotonic()
    log.info(
        "Batch ingest tick: schedule=%s max=%d budget=%.0fs",
        INGEST_SCHEDULE, BATCH_MAX_FILES, BATCH_TIME_BUDGET_SEC,
    )

    try:
        # List a little more than BATCH_MAX_FILES so we still fill the batch
        # after skipping blobs that don't match the layout convention.
        all_names = storage_client.list_blob_names(
            container=SOURCE_CONTAINER,
            max_results=BATCH_MAX_FILES * 4,
        )
    except Exception:
        log.exception("Failed to list blobs in %s", SOURCE_CONTAINER)
        return

    candidates: list[tuple[str, str, str, str]] = []
    skipped_layout = 0
    for full_name in all_names:
        # `list_blobs` returns names relative to the container, not prefixed
        # with `<container>/`, so we adapt the path parser accordingly.
        parsed = _parse_relative_blob_path(f"{SOURCE_CONTAINER}/{full_name}")
        if parsed is None:
            skipped_layout += 1
            continue
        candidates.append(parsed)
        if len(candidates) >= BATCH_MAX_FILES:
            break

    log.info(
        "Found %d candidate blob(s) (skipped %d non-conforming names)",
        len(candidates), skipped_layout,
    )

    summary = _BatchSummary()
    for relative, usecase, analyzer_name, file_name in candidates:
        if time.monotonic() - started > BATCH_TIME_BUDGET_SEC:
            log.warning(
                "Time budget reached after %d file(s); deferring rest to next tick",
                summary.total,
            )
            summary.deferred += 1
            break

        outcome = _process_one_blob(
            relative=relative,
            usecase=usecase,
            analyzer_name=analyzer_name,
            file_name=file_name,
        )
        summary.record(outcome)

    elapsed = time.monotonic() - started
    log.info(
        "Batch ingest done: processed=%d failed=%d skipped=%d deferred=%d elapsed=%.1fs",
        summary.processed, summary.failed, summary.skipped, summary.deferred, elapsed,
    )


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

@dataclass
class _BatchSummary:
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    deferred: int = 0

    @property
    def total(self) -> int:
        return self.processed + self.failed + self.skipped

    def record(self, outcome: str) -> None:
        if outcome == "processed":
            self.processed += 1
        elif outcome == "failed":
            self.failed += 1
        else:
            self.skipped += 1


def _process_one_blob(
    *,
    relative: str,
    usecase: str,
    analyzer_name: str,
    file_name: str,
) -> str:
    """Process a single blob end-to-end. Returns "processed", "failed", or "skipped"."""

    log.info(
        "Ingesting blob: usecase=%s analyzer=%s file=%s",
        usecase, analyzer_name, file_name,
    )

    # Best-effort exclusive lease so we don't race another worker (or read a
    # blob that's still being uploaded).
    with storage_client.acquire_short_lease(
        container=SOURCE_CONTAINER, blob_name=relative,
    ) as lease:
        if lease is None:
            return "skipped"

        # ----- read -----------------------------------------------------------
        try:
            raw = storage_client.read_blob_bytes(
                container=SOURCE_CONTAINER, blob_name=relative,
            )
        except Exception as exc:
            _handle_failure(
                relative=relative,
                usecase=usecase, analyzer_name=analyzer_name,
                kind="ReadError", err=exc,
                source_lease_id=lease.id,
            )
            return "failed"

        # ----- parse JSON -----------------------------------------------------
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            _handle_failure(
                relative=relative,
                usecase=usecase, analyzer_name=analyzer_name,
                kind="ParseError", err=exc,
                source_lease_id=lease.id,
            )
            return "failed"

        # ----- parse + flatten -----------------------------------------------
        try:
            documents = ingestion.parse_content_understanding_json(
                payload, default_document_name=file_name,
            )
        except Exception as exc:
            _handle_failure(
                relative=relative,
                usecase=usecase, analyzer_name=analyzer_name,
                kind="SchemaError", err=exc,
                source_lease_id=lease.id,
            )
            return "failed"

        # ----- write to SQL --------------------------------------------------
        blob_path = f"{SOURCE_CONTAINER}/{relative}"
        try:
            document_ids: list[int] = []
            for doc in documents:
                doc_id = sql_client.write_document(
                    doc,
                    usecase=usecase,
                    analyzer_name=analyzer_name,
                    blob_path=blob_path,
                )
                document_ids.append(doc_id)
        except Exception as exc:
            _handle_failure(
                relative=relative,
                usecase=usecase, analyzer_name=analyzer_name,
                kind="SqlError", err=exc,
                source_lease_id=lease.id,
            )
            return "failed"

        # ----- move to processed ---------------------------------------------
        try:
            move = storage_client.move_blob(
                source_container=SOURCE_CONTAINER,
                source_blob=relative,
                destination_container=PROCESSED_CONTAINER,
                destination_blob=relative,        # preserve <usecase>/<analyzer>/<file>
                source_lease_id=lease.id,
            )
        except Exception as exc:
            # SQL is already populated. Log the move failure but don't lose data —
            # leave the original blob in place so the next tick can retry.
            sql_client.log_error(
                blob_path=blob_path,
                usecase=usecase, analyzer_name=analyzer_name,
                error_kind="MoveError",
                error_message=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            )
            log.exception("Move to processed failed for %s", relative)
            return "failed"

        # Patch processed URL into the document row(s).
        for doc_id in document_ids:
            try:
                sql_client.update_processed_url(doc_id, move.destination_url)
            except Exception:  # pragma: no cover
                log.exception("Failed to update processed_blob_url for document_id=%s", doc_id)

        log.info(
            "Done. file=%s -> %s (%d document(s) inserted)",
            relative, move.destination_url, len(document_ids),
        )
        return "processed"


# ---------------------------------------------------------------------------
# Failure path: move to FAILED container with .error.txt
# ---------------------------------------------------------------------------

def _parse_relative_blob_path(full_name: str) -> tuple[str, str, str, str] | None:
    # Strip leading "<container>/" → "<usecase>/<analyzer>/<file>".
    relative = full_name.split("/", 1)[1] if "/" in full_name else full_name
    parts = relative.split("/")
    if len(parts) != 3 or not parts[2].lower().endswith(".json"):
        return None
    usecase, analyzer_name, file_name = parts
    return relative, usecase, analyzer_name, file_name

def _handle_failure(
    *,
    relative: str,
    usecase: str,
    analyzer_name: str,
    kind: str,
    err: BaseException,
    source_lease_id: str | None = None,
) -> None:
    tb = traceback.format_exc()
    log.exception("[%s] Ingestion failed for %s: %s", kind, relative, err)

    # Record in SQL (best effort).
    sql_client.log_error(
        blob_path=f"{SOURCE_CONTAINER}/{relative}",
        usecase=usecase,
        analyzer_name=analyzer_name,
        error_kind=kind,
        error_message=f"{type(err).__name__}: {err}\n{tb}",
    )

    # Write a sibling .error.txt in the FAILED container, then move the blob.
    try:
        storage_client.write_text_blob(
            container=FAILED_CONTAINER,
            blob_name=f"{relative}.error.txt",
            text=f"[{kind}] {type(err).__name__}: {err}\n\n{tb}",
        )
        storage_client.move_blob(
            source_container=SOURCE_CONTAINER,
            source_blob=relative,
            destination_container=FAILED_CONTAINER,
            destination_blob=relative,
            source_lease_id=source_lease_id,
        )
    except Exception:  # pragma: no cover
        log.exception("Could not quarantine failed blob %s", relative)
