"""
SQL Server client backed by `pyodbc` + Microsoft Entra Managed Identity.

Uses the `SQL_COPT_SS_ACCESS_TOKEN` connection attribute (1256) so we never
pass a password and never need ODBC to know about Entra at all.
"""

from __future__ import annotations

import json
import logging
import os
import struct
from contextlib import contextmanager
from typing import Iterable

import pyodbc
from azure.identity import DefaultAzureCredential

from ingestion import FieldRow, ParsedDocument
from quality_check import QualityReport

log = logging.getLogger(__name__)

_SQL_COPT_SS_ACCESS_TOKEN = 1256
_TOKEN_SCOPE = "https://database.windows.net/.default"

_DRIVER = "{ODBC Driver 18 for SQL Server}"

# Re-use one credential per Function instance (cheap, thread-safe, caches token).
_credential: DefaultAzureCredential | None = None


def _get_credential() -> DefaultAzureCredential:
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return _credential


def _access_token_struct() -> bytes:
    token = _get_credential().get_token(_TOKEN_SCOPE).token.encode("utf-16-le")
    return struct.pack(f"=I{len(token)}s", len(token), token)


def _connection_string() -> str:
    server = os.environ["SQL_SERVER"]
    database = os.environ["SQL_DATABASE"]
    return (
        f"Driver={_DRIVER};"
        f"Server=tcp:{server},1433;"
        f"Database={database};"
        "Encrypt=yes;"
        "TrustServerCertificate=no;"
        "Connection Timeout=30;"
    )


@contextmanager
def connect():
    """Yield a pyodbc connection authenticated with the Function's Managed Identity."""
    conn = pyodbc.connect(
        _connection_string(),
        attrs_before={_SQL_COPT_SS_ACCESS_TOKEN: _access_token_struct()},
    )
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# High-level operations
# ---------------------------------------------------------------------------

def write_document(
    doc: ParsedDocument,
    *,
    usecase: str,
    analyzer_name: str,
    blob_path: str,
    processed_blob_url: str | None = None,
) -> int:
    """Insert one document + all its fields in a single transaction.

    Returns the new `document_id`. Re-ingesting the same (blob_path, content_path)
    replaces the prior rows (see `cu.usp_UpsertDocument`).
    """
    with connect() as conn:
        conn.autocommit = False
        try:
            cur = conn.cursor()

            # ---- header ------------------------------------------------------
            doc_id_param = cur.execute(
                """
                DECLARE @id BIGINT;
                EXEC cu.usp_UpsertDocument
                    @usecase           = ?,
                    @analyzer_name     = ?,
                    @analyzer_id       = ?,
                    @document_name     = ?,
                    @blob_path         = ?,
                    @content_path      = ?,
                    @mime_type         = ?,
                    @source_created_at = ?,
                    @api_version       = ?,
                    @operation_id      = ?,
                    @status            = ?,
                    @document_id       = @id OUTPUT;
                SELECT @id;
                """,
                usecase,
                analyzer_name,
                doc.analyzer_id,
                doc.document_name,
                blob_path,
                doc.content_path,
                doc.mime_type,
                doc.source_created_at,
                doc.api_version,
                doc.operation_id,
                doc.status,
            ).fetchval()

            document_id = int(doc_id_param)

            # ---- field rows --------------------------------------------------
            if doc.fields:
                _bulk_insert_fields(cur, document_id, doc.fields)

            # ---- finalize (stats + processed url) ----------------------------
            cur.execute(
                "EXEC cu.usp_FinalizeDocument @document_id = ?, @processed_blob_url = ?;",
                document_id,
                processed_blob_url,
            )

            conn.commit()
            log.info(
                "Inserted document_id=%s with %d field(s) for blob=%s",
                document_id, len(doc.fields), blob_path,
            )
            return document_id
        except Exception:
            conn.rollback()
            raise


def update_processed_url(document_id: int, processed_blob_url: str) -> None:
    """Patch the processed URL after a successful blob move."""
    with connect() as conn:
        conn.execute(
            "UPDATE cu.Documents SET processed_blob_url = ? WHERE document_id = ?;",
            processed_blob_url,
            document_id,
        )
        conn.commit()


def log_error(
    *,
    blob_path: str,
    error_kind: str,
    error_message: str,
    usecase: str | None = None,
    analyzer_name: str | None = None,
) -> None:
    """Best-effort error log into cu.IngestionErrors. Never raises."""
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO cu.IngestionErrors
                    (blob_path, usecase, analyzer_name, error_kind, error_message)
                VALUES (?, ?, ?, ?, ?);
                """,
                blob_path,
                usecase,
                analyzer_name,
                error_kind,
                error_message[:4000],
            )
            conn.commit()
    except Exception as exc:  # pragma: no cover
        log.exception("Failed to persist IngestionErrors row: %s", exc)


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

def _bulk_insert_fields(cur: pyodbc.Cursor, document_id: int, rows: Iterable[FieldRow]) -> None:
    cur.fast_executemany = True
    cur.executemany(
        """
        INSERT INTO cu.DocumentFields
            (document_id, field_path, field_name, parent_path, array_index,
             field_type, value_string, value_number, value_integer, value_date,
             value_boolean, currency_code, confidence, span_offset, span_length)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        [
            (
                document_id,
                r.field_path,
                r.field_name,
                r.parent_path,
                r.array_index,
                r.field_type,
                r.value_string,
                r.value_number,
                r.value_integer,
                r.value_date,
                r.value_boolean,
                r.currency_code,
                r.confidence,
                r.span_offset,
                r.span_length,
            )
            for r in rows
        ],
    )


# ---------------------------------------------------------------------------
# Pre-process quality check persistence
# ---------------------------------------------------------------------------

def write_preprocess_check(
    report: QualityReport,
    *,
    usecase: str,
    analyzer_name: str,
    blob_path: str,
    file_name: str,
) -> int:
    """Insert one PreProcessChecks row + all its issue rows in a single transaction.

    Returns the new ``check_id``. The caller uses this id later to
    (a) update the CU submission outcome via :func:`update_preprocess_cu_outcome`
    and (b) back-link the resulting ``cu.Documents`` row via
    :func:`set_document_preprocess_check_id`.
    """
    with connect() as conn:
        conn.autocommit = False
        try:
            cur = conn.cursor()

            check_id = cur.execute(
                """
                DECLARE @id BIGINT;
                EXEC cu.usp_InsertPreProcessCheck
                    @blob_path       = ?,
                    @usecase         = ?,
                    @analyzer_name   = ?,
                    @file_name       = ?,
                    @extension       = ?,
                    @detected_kind   = ?,
                    @file_size_bytes = ?,
                    @mode            = ?,
                    @passed          = ?,
                    @score           = ?,
                    @band            = ?,
                    @error_count     = ?,
                    @warning_count   = ?,
                    @info_count      = ?,
                    @metadata_json   = ?,
                    @check_id        = @id OUTPUT;
                SELECT @id;
                """,
                blob_path,
                usecase,
                analyzer_name,
                file_name,
                report.extension,
                report.detected_kind,
                report.file_size_bytes,
                report.mode,
                1 if report.passed else 0,
                report.score,
                report.band,
                report.error_count,
                report.warning_count,
                report.info_count,
                json.dumps(report.metadata, default=str) if report.metadata else None,
            ).fetchval()

            check_id = int(check_id)

            if report.issues:
                cur.fast_executemany = True
                cur.executemany(
                    """
                    INSERT INTO cu.PreProcessIssues
                        (check_id, code, severity, message, details_json)
                    VALUES (?, ?, ?, ?, ?);
                    """,
                    [
                        (
                            check_id,
                            issue.code,
                            issue.severity.value,
                            issue.message[:2000],
                            json.dumps(issue.details, default=str) if issue.details else None,
                        )
                        for issue in report.issues
                    ],
                )

            conn.commit()
            log.info(
                "Inserted PreProcessChecks check_id=%s for %s (passed=%s score=%d issues=%d)",
                check_id, blob_path, report.passed, report.score, len(report.issues),
            )
            return check_id
        except Exception:
            conn.rollback()
            raise


def update_preprocess_cu_outcome(
    check_id: int,
    *,
    submitted_to_cu: bool,
    cu_status: str | None = None,
    cu_operation_location: str | None = None,
    cu_error_message: str | None = None,
    routed_to_blob_path: str | None = None,
    cu_result_blob_path: str | None = None,
) -> None:
    """Patch the CU submission columns on a PreProcessChecks row."""
    with connect() as conn:
        conn.execute(
            """
            EXEC cu.usp_UpdatePreProcessCuOutcome
                @check_id              = ?,
                @submitted_to_cu       = ?,
                @cu_operation_location = ?,
                @cu_status             = ?,
                @cu_error_message      = ?,
                @routed_to_blob_path   = ?,
                @cu_result_blob_path   = ?;
            """,
            check_id,
            1 if submitted_to_cu else 0,
            cu_operation_location,
            cu_status,
            cu_error_message[:4000] if cu_error_message else None,
            routed_to_blob_path,
            cu_result_blob_path,
        )
        conn.commit()


def set_document_preprocess_check_id(document_id: int, check_id: int) -> None:
    """Back-link the ingested document to its originating quality check."""
    with connect() as conn:
        conn.execute(
            "UPDATE cu.Documents SET preprocess_check_id = ? WHERE document_id = ?;",
            check_id,
            document_id,
        )
        conn.commit()
