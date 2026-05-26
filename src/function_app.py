"""
Azure Function entry points.

Two timer-driven batch loops cover the end-to-end pipeline:

1. ``preprocess_and_extract_batch`` — scans the ``incoming`` container for raw
   documents (PDF, image, Office, text), runs the local quality checker
   (``quality_check.check_document``), persists the result into
   ``cu.PreProcessChecks`` / ``cu.PreProcessIssues``, and then either:

     * On FAIL  -> moves the raw doc to ``rejected/<usecase>/<analyzer>/<file>.<ext>``
                   and updates the check row with ``cu_status='Skipped'``.
     * On PASS  -> submits the raw bytes to the Content Understanding analyzer,
                   writes the result JSON to ``source/<usecase>/<analyzer>/<stem>.json``
                   (stamped with blob metadata ``preprocesscheckid=<id>``),
                   moves the raw doc to ``processed-raw/<usecase>/<analyzer>/<file>.<ext>``,
                   and updates the check row with ``cu_status='Succeeded'`` /
                   ``cu_result_blob_path=...``.

2. ``ingest_content_understanding_batch`` — scans the ``source`` container for
   CU result JSONs (produced by step 1, or uploaded directly), flattens every
   leaf field into ``cu.Documents`` + ``cu.DocumentFields``, back-links to the
   originating ``cu.PreProcessChecks`` row when the ``preprocesscheckid``
   metadata tag is present, then moves the JSON to ``processed/``.

Both loops are idempotent, lease blobs while they work on them so overlapping
ticks (or a tick + a still-writing uploader) can't race, and respect a soft
time budget so they never bump into the 10-minute host timeout.

Auth: Managed Identity end-to-end (storage + SQL + Content Understanding).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import PurePosixPath

import azure.functions as func

import cu_client
import ingestion
import quality_check
import sql_client
import storage_client

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SOURCE_CONTAINER       = os.getenv("SOURCE_CONTAINER",       "source")
PROCESSED_CONTAINER    = os.getenv("PROCESSED_CONTAINER",    "processed")
FAILED_CONTAINER       = os.getenv("FAILED_CONTAINER",       "failed")
INCOMING_CONTAINER     = os.getenv("INCOMING_CONTAINER",     "incoming")
REJECTED_CONTAINER     = os.getenv("REJECTED_CONTAINER",     "rejected")
PROCESSED_RAW_CONTAINER = os.getenv("PROCESSED_RAW_CONTAINER", "processed-raw")

# NCRONTAB schedules (seconds minutes hours day month weekday, UTC).
INGEST_SCHEDULE        = os.getenv("INGEST_SCHEDULE",        "0 */15 * * * *")
PREPROCESS_SCHEDULE    = os.getenv("PREPROCESS_SCHEDULE",    "0 */15 * * * *")

# Safety caps so a single tick stays well inside the function timeout.
BATCH_MAX_FILES        = int(os.getenv("BATCH_MAX_FILES",       "50"))
BATCH_TIME_BUDGET_SEC  = float(os.getenv("BATCH_TIME_BUDGET_SEC", "540"))

PREPROCESS_BATCH_MAX_FILES   = int(os.getenv("PREPROCESS_BATCH_MAX_FILES",   "20"))
PREPROCESS_TIME_BUDGET_SEC   = float(os.getenv("PREPROCESS_TIME_BUDGET_SEC", "540"))

# Quality-check tuning.
PREPROCESS_MODE   = os.getenv("PREPROCESS_MODE",   "standard").lower()       # standard | pro
PREPROCESS_STRICT = os.getenv("PREPROCESS_STRICT", "false").lower() == "true"  # treat WARNINGs as failures

# Content Understanding.
CU_ENDPOINT     = os.getenv("CU_ENDPOINT", "").strip()        # e.g. https://<rsrc>.cognitiveservices.azure.com
CU_API_VERSION  = os.getenv("CU_API_VERSION", "").strip() or None  # cu_client falls back to its default

# Metadata key we stamp on the CU result JSON so the ingest loop can back-link
# the resulting cu.Documents row to its originating PreProcessChecks row.
_PREPROCESS_CHECK_METADATA_KEY = "preprocesscheckid"

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
        # `list_blobs` returns names relative to the container (e.g.
        # `<usecase>/<analyzer>/<file>.json`) — that is exactly what
        # `_parse_relative_blob_path` expects.
        parsed = _parse_relative_blob_path(full_name)
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
            raw, blob_metadata = storage_client.read_blob_with_metadata(
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

        # Optional back-link to the originating pre-process check.
        preprocess_check_id = _coerce_int(
            blob_metadata.get(_PREPROCESS_CHECK_METADATA_KEY)
        )

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
            if preprocess_check_id is not None:
                try:
                    sql_client.set_document_preprocess_check_id(doc_id, preprocess_check_id)
                except Exception:  # pragma: no cover
                    log.exception(
                        "Failed to back-link document_id=%s to preprocess_check_id=%s",
                        doc_id, preprocess_check_id,
                    )

        log.info(
            "Done. file=%s -> %s (%d document(s) inserted)",
            relative, move.destination_url, len(document_ids),
        )
        return "processed"


# ---------------------------------------------------------------------------
# Failure path: move to FAILED container with .error.txt
# ---------------------------------------------------------------------------

def _parse_relative_blob_path(full_name: str) -> tuple[str, str, str, str] | None:
    # `full_name` is the container-relative blob name returned by
    # ContainerClient.list_blobs(): "<usecase>/<analyzer>/<file>.json".
    relative = full_name
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


def _coerce_int(value: object) -> int | None:
    """Tolerantly coerce a blob-metadata string value into an int."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ===========================================================================
# Pre-process + Content Understanding submission loop
# ===========================================================================

@app.timer_trigger(
    arg_name="timer",
    schedule=PREPROCESS_SCHEDULE,
    run_on_startup=False,
    use_monitor=False,
)
def preprocess_and_extract_batch(timer: func.TimerRequest) -> None:
    """Quality-check raw uploads in ``incoming`` then submit passes to CU."""

    started = time.monotonic()
    log.info(
        "preprocess_and_extract_batch start (incoming=%s strict=%s mode=%s)",
        INCOMING_CONTAINER, PREPROCESS_STRICT, PREPROCESS_MODE,
    )

    if not CU_ENDPOINT:
        log.error("CU_ENDPOINT is not configured; cannot submit to Content Understanding.")
        return

    summary = _BatchSummary()
    try:
        candidates = storage_client.list_blob_names(
            container=INCOMING_CONTAINER, max_results=PREPROCESS_BATCH_MAX_FILES,
        )
    except Exception:
        log.exception("Failed to list blobs in %s", INCOMING_CONTAINER)
        return

    for full_name in candidates:
        if (time.monotonic() - started) >= PREPROCESS_TIME_BUDGET_SEC:
            log.warning(
                "Time budget reached; deferring %d remaining file(s) to next tick.",
                max(0, len(candidates) - summary.total),
            )
            summary.deferred = max(0, len(candidates) - summary.total)
            break

        parsed = _parse_raw_blob_path(full_name)
        if parsed is None:
            log.warning("Skipping unexpected blob layout: %s", full_name)
            summary.record("skipped")
            continue

        relative, usecase, analyzer_name, file_name = parsed
        try:
            outcome = _process_one_raw_blob(
                relative=relative,
                usecase=usecase,
                analyzer_name=analyzer_name,
                file_name=file_name,
            )
        except Exception:  # defensive — keep the loop going
            log.exception("Unhandled error preprocessing %s", relative)
            outcome = "failed"
        summary.record(outcome)

    log.info(
        "preprocess_and_extract_batch done in %.1fs: processed=%d failed=%d skipped=%d deferred=%d",
        time.monotonic() - started,
        summary.processed, summary.failed, summary.skipped, summary.deferred,
    )


def _parse_raw_blob_path(full_name: str) -> tuple[str, str, str, str] | None:
    """``<usecase>/<analyzer>/<file>.<ext>`` (container-relative) -> components."""
    relative = full_name
    parts = relative.split("/")
    if len(parts) != 3 or "." not in parts[2]:
        return None
    usecase, analyzer_name, file_name = parts
    return relative, usecase, analyzer_name, file_name


def _process_one_raw_blob(
    *,
    relative: str,
    usecase: str,
    analyzer_name: str,
    file_name: str,
) -> str:
    """Quality-check one raw doc and, if it passes, submit it to CU.

    Returns "processed", "failed", or "skipped".
    """

    log.info(
        "Pre-processing: usecase=%s analyzer=%s file=%s",
        usecase, analyzer_name, file_name,
    )
    blob_path = f"{INCOMING_CONTAINER}/{relative}"

    with storage_client.acquire_short_lease(
        container=INCOMING_CONTAINER, blob_name=relative,
    ) as lease:
        if lease is None:
            return "skipped"

        # ----- download bytes to a temp file (quality_check works on paths) ---
        suffix = PurePosixPath(file_name).suffix or ".bin"
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp_path = tmp.name
        tmp.close()
        try:
            try:
                storage_client.download_blob_to_file(
                    container=INCOMING_CONTAINER, blob_name=relative, destination=tmp_path,
                )
                with open(tmp_path, "rb") as f:
                    raw_bytes = f.read()
            except Exception as exc:
                log.exception("Failed to download raw blob %s", relative)
                _quarantine_raw(
                    relative=relative, usecase=usecase, analyzer_name=analyzer_name,
                    kind="ReadError", err=exc, lease_id=lease.id,
                )
                return "failed"

            # ----- run quality check ----------------------------------------
            try:
                report = quality_check.check_document(tmp_path, mode=PREPROCESS_MODE)
            except Exception as exc:
                log.exception("Quality check raised for %s", relative)
                _quarantine_raw(
                    relative=relative, usecase=usecase, analyzer_name=analyzer_name,
                    kind="QualityCheckError", err=exc, lease_id=lease.id,
                )
                return "failed"

            # ----- persist the check ----------------------------------------
            try:
                check_id = sql_client.write_preprocess_check(
                    report,
                    usecase=usecase,
                    analyzer_name=analyzer_name,
                    blob_path=blob_path,
                    file_name=file_name,
                )
            except Exception as exc:
                log.exception("Failed to persist PreProcessChecks row for %s", relative)
                _quarantine_raw(
                    relative=relative, usecase=usecase, analyzer_name=analyzer_name,
                    kind="SqlError", err=exc, lease_id=lease.id,
                )
                return "failed"

            # Did it pass our quality bar?
            quality_passed = report.passed and not (
                PREPROCESS_STRICT and report.warning_count > 0
            )

            if not quality_passed:
                return _route_to_rejected(
                    relative=relative, usecase=usecase, analyzer_name=analyzer_name,
                    file_name=file_name, report=report, check_id=check_id,
                    lease_id=lease.id, reason="QualityCheckFailed",
                )

            # ----- submit to Content Understanding --------------------------
            try:
                cu_result = cu_client.analyze_file(
                    endpoint=CU_ENDPOINT,
                    analyzer_id=analyzer_name,
                    file_bytes=raw_bytes,
                    file_name=file_name,
                    api_version=CU_API_VERSION,
                )
            except cu_client.CuTimeoutError as exc:
                log.error("CU analyze timed out for %s: %s", relative, exc)
                return _route_to_rejected(
                    relative=relative, usecase=usecase, analyzer_name=analyzer_name,
                    file_name=file_name, report=report, check_id=check_id,
                    lease_id=lease.id, reason="CuTimeout",
                    cu_status="Timeout", cu_error_message=str(exc),
                    cu_operation_location=getattr(exc, "operation_location", None),
                )
            except Exception as exc:
                log.exception("CU analyze failed for %s", relative)
                return _route_to_rejected(
                    relative=relative, usecase=usecase, analyzer_name=analyzer_name,
                    file_name=file_name, report=report, check_id=check_id,
                    lease_id=lease.id, reason="CuError",
                    cu_status="Failed", cu_error_message=f"{type(exc).__name__}: {exc}",
                )

            if cu_result.status != "Succeeded" or cu_result.result_json is None:
                return _route_to_rejected(
                    relative=relative, usecase=usecase, analyzer_name=analyzer_name,
                    file_name=file_name, report=report, check_id=check_id,
                    lease_id=lease.id, reason="CuNonSuccess",
                    cu_status=cu_result.status or "Failed",
                    cu_error_message=cu_result.error_message,
                    cu_operation_location=cu_result.operation_location,
                )

            # ----- write CU JSON to source/ + move raw to processed-raw/ ----
            stem = PurePosixPath(file_name).stem
            cu_json_relative = f"{usecase}/{analyzer_name}/{stem}.json"
            try:
                cu_json_url = storage_client.write_json_blob(
                    container=SOURCE_CONTAINER,
                    blob_name=cu_json_relative,
                    payload=cu_result.result_json,
                    metadata={_PREPROCESS_CHECK_METADATA_KEY: str(check_id)},
                )
            except Exception as exc:
                log.exception("Failed to write CU result JSON for %s", relative)
                return _route_to_rejected(
                    relative=relative, usecase=usecase, analyzer_name=analyzer_name,
                    file_name=file_name, report=report, check_id=check_id,
                    lease_id=lease.id, reason="StorageError",
                    cu_status="Succeeded",
                    cu_error_message=f"CU succeeded but writing result JSON failed: {exc}",
                    cu_operation_location=cu_result.operation_location,
                )

            try:
                move = storage_client.move_blob(
                    source_container=INCOMING_CONTAINER,
                    source_blob=relative,
                    destination_container=PROCESSED_RAW_CONTAINER,
                    destination_blob=relative,
                    source_lease_id=lease.id,
                )
                processed_raw_url = move.destination_url
            except Exception:
                # CU succeeded and the JSON is written; we just couldn't move the
                # raw doc out of incoming. Log and continue — the next tick will
                # retry, and at worst we'll write a duplicate CU JSON.
                log.exception("Failed to move raw blob %s after successful CU", relative)
                processed_raw_url = None

            try:
                sql_client.update_preprocess_cu_outcome(
                    check_id,
                    submitted_to_cu=True,
                    cu_status="Succeeded",
                    cu_operation_location=cu_result.operation_location,
                    routed_to_blob_path=processed_raw_url
                        or f"{PROCESSED_RAW_CONTAINER}/{relative}",
                    cu_result_blob_path=cu_json_url,
                )
            except Exception:  # pragma: no cover
                log.exception(
                    "CU succeeded but updating PreProcessChecks check_id=%s failed", check_id
                )

            log.info(
                "Pre-process OK: %s -> CU JSON %s (check_id=%s)",
                relative, cu_json_url, check_id,
            )
            return "processed"
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _route_to_rejected(
    *,
    relative: str,
    usecase: str,
    analyzer_name: str,
    file_name: str,
    report: quality_check.QualityReport,
    check_id: int,
    lease_id: str | None,
    reason: str,
    cu_status: str | None = None,
    cu_error_message: str | None = None,
    cu_operation_location: str | None = None,
) -> str:
    """Move the raw doc to ``rejected/``, drop a JSON report next to it, and
    update the PreProcessChecks row. Returns "failed"."""

    report_relative = f"{relative}.report.json"
    routed_url: str | None = None

    # Best-effort write of the human-readable report.
    try:
        storage_client.write_json_blob(
            container=REJECTED_CONTAINER,
            blob_name=report_relative,
            payload={
                "reason": reason,
                "cu_status": cu_status,
                "cu_error_message": cu_error_message,
                "report": report.to_dict(),
            },
        )
    except Exception:  # pragma: no cover
        log.exception("Failed to write rejection report for %s", relative)

    # Move the raw blob.
    try:
        move = storage_client.move_blob(
            source_container=INCOMING_CONTAINER,
            source_blob=relative,
            destination_container=REJECTED_CONTAINER,
            destination_blob=relative,
            source_lease_id=lease_id,
        )
        routed_url = move.destination_url
    except Exception:
        log.exception("Failed to move rejected blob %s", relative)

    # Patch the SQL row.
    try:
        sql_client.update_preprocess_cu_outcome(
            check_id,
            submitted_to_cu=cu_status is not None,
            cu_status=cu_status or "Skipped",
            cu_operation_location=cu_operation_location,
            cu_error_message=cu_error_message
                or f"{reason}: quality score={report.score} band={report.band}",
            routed_to_blob_path=routed_url
                or f"{REJECTED_CONTAINER}/{relative}",
        )
    except Exception:  # pragma: no cover
        log.exception("Failed to update PreProcessChecks check_id=%s", check_id)

    # Mirror to IngestionErrors so the existing reporting view sees it too.
    try:
        sql_client.log_error(
            blob_path=f"{INCOMING_CONTAINER}/{relative}",
            usecase=usecase,
            analyzer_name=analyzer_name,
            error_kind=reason,
            error_message=cu_error_message
                or f"Quality check failed: score={report.score} band={report.band} "
                   f"errors={report.error_count} warnings={report.warning_count}",
        )
    except Exception:  # pragma: no cover
        pass

    return "failed"


def _quarantine_raw(
    *,
    relative: str,
    usecase: str,
    analyzer_name: str,
    kind: str,
    err: BaseException,
    lease_id: str | None,
) -> None:
    """Hard-failure path for raw blobs (read/quality-check/sql exceptions).

    Moves the blob to ``rejected/`` with a sibling ``.error.txt`` and logs it
    in cu.IngestionErrors. Mirrors :func:`_handle_failure` for the ingest loop.
    """
    tb = traceback.format_exc()
    try:
        storage_client.write_text_blob(
            container=REJECTED_CONTAINER,
            blob_name=f"{relative}.error.txt",
            text=f"[{kind}] {type(err).__name__}: {err}\n\n{tb}",
        )
        storage_client.move_blob(
            source_container=INCOMING_CONTAINER,
            source_blob=relative,
            destination_container=REJECTED_CONTAINER,
            destination_blob=relative,
            source_lease_id=lease_id,
        )
    except Exception:  # pragma: no cover
        log.exception("Could not quarantine raw blob %s", relative)

    try:
        sql_client.log_error(
            blob_path=f"{INCOMING_CONTAINER}/{relative}",
            usecase=usecase,
            analyzer_name=analyzer_name,
            error_kind=kind,
            error_message=f"{type(err).__name__}: {err}\n{tb}",
        )
    except Exception:  # pragma: no cover
        pass
