"""
Azure Function entry point.

Trigger: a new blob landing in the source container under the convention

    <SOURCE_CONTAINER>/<usecase>/<analyzer>/<file>.json

For each file:

  1. Read + parse the JSON.
  2. Flatten every leaf field into rows and INSERT into Azure SQL.
  3. MOVE the file to <PROCESSED_CONTAINER>/<usecase>/<analyzer>/<file>.json.
  4. On any failure: MOVE to <FAILED_CONTAINER>/<usecase>/<analyzer>/<file>.json
     and write a sibling `<file>.json.error.txt` with the traceback,
     then record the failure in cu.IngestionErrors.

Auth: Managed Identity end-to-end (storage + SQL).
"""

from __future__ import annotations

import json
import logging
import os
import traceback

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

# Blob trigger path: matches `<source>/<usecase>/<analyzer>/<name>`.
# {name} binds to the file name (with extension).
_BLOB_PATH = f"{SOURCE_CONTAINER}/{{usecase}}/{{analyzer}}/{{name}}"

app = func.FunctionApp()


@app.blob_trigger(
    arg_name="blob",
    path=_BLOB_PATH,
    connection="AzureWebJobsStorage",
)
def ingest_content_understanding_json(blob: func.InputStream) -> None:
    """Process one blob. Bindings inject usecase/analyzer/name from the path."""

    full_name = blob.name or ""                 # e.g. "source/invoices/contoso/foo.json"
    parsed = _parse_relative_blob_path(full_name)
    if parsed is None:
        log.warning(
            "Skipping blob %s — expected layout <usecase>/<analyzer>/<file>.json",
            full_name,
        )
        return

    relative, usecase, analyzer_name, file_name = parsed
    log.info(
        "Ingesting blob: usecase=%s analyzer=%s file=%s bytes=%s",
        usecase, analyzer_name, file_name, blob.length,
    )

    try:
        payload = json.loads(blob.read().decode("utf-8"))
    except Exception as exc:
        _handle_failure(
            relative=relative,
            usecase=usecase, analyzer_name=analyzer_name,
            kind="ParseError", err=exc,
        )
        return

    # ----- parse + flatten ---------------------------------------------------
    try:
        documents = ingestion.parse_content_understanding_json(
            payload, default_document_name=file_name,
        )
    except Exception as exc:
        _handle_failure(
            relative=relative,
            usecase=usecase, analyzer_name=analyzer_name,
            kind="SchemaError", err=exc,
        )
        return

    # ----- write to SQL ------------------------------------------------------
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
        )
        return

    # ----- move to processed -------------------------------------------------
    try:
        move = storage_client.move_blob(
            source_container=SOURCE_CONTAINER,
            source_blob=relative,
            destination_container=PROCESSED_CONTAINER,
            destination_blob=relative,            # preserve <usecase>/<analyzer>/<file>
        )
    except Exception as exc:
        # SQL is already populated. Log the move failure but don't lose data —
        # leave the original blob in place so the next run can retry.
        sql_client.log_error(
            blob_path=blob_path,
            usecase=usecase, analyzer_name=analyzer_name,
            error_kind="MoveError",
            error_message=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        )
        log.exception("Move to processed failed for %s", relative)
        return

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
        )
    except Exception:  # pragma: no cover
        log.exception("Could not quarantine failed blob %s", relative)
