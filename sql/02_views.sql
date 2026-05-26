-- =============================================================================
-- Reporting views — Power BI connects to these.
-- =============================================================================
-- Pull these in via Get Data > Azure SQL Database, then build visuals on top.
-- All views live in the `cu` schema. Refresh after schema changes.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- vw_DocumentFields
-- Main fact table for Power BI. One row per extracted field, joined with all
-- document-level dimensions (usecase, analyzer, document name, etc.).
-- ---------------------------------------------------------------------------
CREATE OR ALTER VIEW cu.vw_DocumentFields
AS
SELECT
    f.field_id,
    d.document_id,
    d.usecase,
    d.analyzer_name,
    d.analyzer_id,
    d.document_name,
    d.blob_path,
    d.processed_blob_url,
    d.content_path,
    d.mime_type,
    d.source_created_at,
    d.ingested_at,
    f.field_path,
    f.field_name,
    f.parent_path,
    f.array_index,
    f.field_type,
    f.value_string,
    f.value_number,
    f.value_integer,
    f.value_date,
    f.value_boolean,
    f.currency_code,
    f.confidence,
    -- Reporting buckets you can slice on directly in Power BI
    CASE
        WHEN f.confidence IS NULL          THEN N'unknown'
        WHEN f.confidence >= 0.90          THEN N'high'
        WHEN f.confidence >= 0.70          THEN N'medium'
        ELSE                                    N'low'
    END AS confidence_bucket,
    CAST(d.ingested_at AS DATE) AS ingested_date
FROM cu.DocumentFields f
JOIN cu.Documents      d ON d.document_id = f.document_id;
GO

-- ---------------------------------------------------------------------------
-- vw_DocumentSummary
-- One row per ingested document — totals, averages, confidence quality.
-- ---------------------------------------------------------------------------
CREATE OR ALTER VIEW cu.vw_DocumentSummary
AS
SELECT
    d.document_id,
    d.usecase,
    d.analyzer_name,
    d.analyzer_id,
    d.document_name,
    d.blob_path,
    d.processed_blob_url,
    d.mime_type,
    d.source_created_at,
    d.ingested_at,
    CAST(d.ingested_at AS DATE) AS ingested_date,
    d.field_count,
    d.avg_confidence,
    d.min_confidence,
    d.max_confidence,
    -- count of low-confidence fields (< 0.7) per doc
    (SELECT COUNT(*) FROM cu.DocumentFields f
      WHERE f.document_id = d.document_id AND f.confidence < 0.70)  AS low_confidence_field_count,
    (SELECT COUNT(*) FROM cu.DocumentFields f
      WHERE f.document_id = d.document_id AND f.confidence IS NULL) AS no_confidence_field_count
FROM cu.Documents d;
GO

-- ---------------------------------------------------------------------------
-- vw_LowConfidenceFields
-- Review queue: every field with confidence < 0.70. Useful as a Power BI table.
-- ---------------------------------------------------------------------------
CREATE OR ALTER VIEW cu.vw_LowConfidenceFields
AS
SELECT
    d.usecase,
    d.analyzer_name,
    d.document_name,
    d.blob_path,
    f.field_path,
    f.field_name,
    f.field_type,
    f.value_string,
    f.confidence,
    d.ingested_at
FROM cu.DocumentFields f
JOIN cu.Documents      d ON d.document_id = f.document_id
WHERE f.confidence < 0.70;
GO

-- ---------------------------------------------------------------------------
-- vw_FieldStatsByAnalyzer
-- Per (usecase, analyzer, field) confidence stats. Great for trending and
-- spotting analyzers / fields that consistently underperform.
-- ---------------------------------------------------------------------------
CREATE OR ALTER VIEW cu.vw_FieldStatsByAnalyzer
AS
SELECT
    d.usecase,
    d.analyzer_name,
    f.field_name,
    f.field_type,
    COUNT(*)                                            AS observations,
    AVG(f.confidence)                                   AS avg_confidence,
    MIN(f.confidence)                                   AS min_confidence,
    MAX(f.confidence)                                   AS max_confidence,
    SUM(CASE WHEN f.confidence < 0.70 THEN 1 ELSE 0 END) AS low_count,
    SUM(CASE WHEN f.confidence >= 0.90 THEN 1 ELSE 0 END) AS high_count,
    MIN(d.ingested_at)                                  AS first_seen,
    MAX(d.ingested_at)                                  AS last_seen
FROM cu.DocumentFields f
JOIN cu.Documents      d ON d.document_id = f.document_id
WHERE f.confidence IS NOT NULL
GROUP BY d.usecase, d.analyzer_name, f.field_name, f.field_type;
GO

-- ---------------------------------------------------------------------------
-- vw_DailyIngestion
-- One row per (date, usecase, analyzer). Lets Power BI plot volume + quality
-- over time without needing measures up front.
-- ---------------------------------------------------------------------------
CREATE OR ALTER VIEW cu.vw_DailyIngestion
AS
SELECT
    CAST(d.ingested_at AS DATE) AS ingested_date,
    d.usecase,
    d.analyzer_name,
    COUNT(*)                  AS document_count,
    SUM(d.field_count)        AS total_fields,
    AVG(d.avg_confidence)     AS avg_confidence,
    MIN(d.min_confidence)     AS worst_field_confidence
FROM cu.Documents d
GROUP BY CAST(d.ingested_at AS DATE), d.usecase, d.analyzer_name;
GO

-- ---------------------------------------------------------------------------
-- vw_PreProcessChecks
-- Flat view of all quality checks. One row per inspected raw document, with
-- pass/fail, score, CU submission outcome, and (when CU succeeded) the
-- extracted document_id + average confidence.
-- ---------------------------------------------------------------------------
CREATE OR ALTER VIEW cu.vw_PreProcessChecks
AS
SELECT
    c.check_id,
    c.blob_path,
    c.usecase,
    c.analyzer_name,
    c.file_name,
    c.extension,
    c.detected_kind,
    c.file_size_bytes,
    c.mode,
    c.passed,
    c.score,
    c.band,
    c.error_count,
    c.warning_count,
    c.info_count,
    c.submitted_to_cu,
    c.cu_status,
    c.cu_operation_location,
    c.cu_error_message,
    c.routed_to_blob_path,
    c.cu_result_blob_path,
    c.checked_at,
    c.completed_at,
    CAST(c.checked_at AS DATE) AS checked_date,
    d.document_id,
    d.field_count       AS extracted_field_count,
    d.avg_confidence    AS extracted_avg_confidence,
    d.min_confidence    AS extracted_min_confidence
FROM cu.PreProcessChecks c
LEFT JOIN cu.Documents d ON d.preprocess_check_id = c.check_id;
GO

-- ---------------------------------------------------------------------------
-- vw_PreProcessIssues
-- Flat issue feed. One row per ERROR/WARNING/INFO finding, joined with the
-- parent check so Power BI can slice on usecase/analyzer/file.
-- ---------------------------------------------------------------------------
CREATE OR ALTER VIEW cu.vw_PreProcessIssues
AS
SELECT
    i.issue_id,
    i.check_id,
    c.blob_path,
    c.usecase,
    c.analyzer_name,
    c.file_name,
    c.detected_kind,
    c.mode,
    c.passed       AS check_passed,
    c.score        AS check_score,
    c.band         AS check_band,
    i.code,
    i.severity,
    i.message,
    i.details_json,
    c.checked_at,
    CAST(c.checked_at AS DATE) AS checked_date
FROM cu.PreProcessIssues i
JOIN cu.PreProcessChecks  c ON c.check_id = i.check_id;
GO

-- ---------------------------------------------------------------------------
-- vw_RejectedDocuments
-- Review queue: every raw document that the quality checker REJECTED before
-- being sent to Content Understanding.
-- ---------------------------------------------------------------------------
CREATE OR ALTER VIEW cu.vw_RejectedDocuments
AS
SELECT
    c.check_id,
    c.blob_path,
    c.routed_to_blob_path,
    c.usecase,
    c.analyzer_name,
    c.file_name,
    c.extension,
    c.detected_kind,
    c.file_size_bytes,
    c.score,
    c.band,
    c.error_count,
    c.warning_count,
    c.metadata_json,
    c.checked_at
FROM cu.PreProcessChecks c
WHERE c.passed = 0;
GO

-- ---------------------------------------------------------------------------
-- vw_PreProcessDailySummary
-- Daily roll-up: how many docs were inspected, how many passed, how many
-- made it through CU, average quality score, top issue code.
-- ---------------------------------------------------------------------------
CREATE OR ALTER VIEW cu.vw_PreProcessDailySummary
AS
SELECT
    CAST(c.checked_at AS DATE)                                                   AS checked_date,
    c.usecase,
    c.analyzer_name,
    COUNT(*)                                                                     AS total_inspected,
    SUM(CASE WHEN c.passed = 1 THEN 1 ELSE 0 END)                                AS passed_count,
    SUM(CASE WHEN c.passed = 0 THEN 1 ELSE 0 END)                                AS rejected_count,
    SUM(CASE WHEN c.cu_status = N'Succeeded' THEN 1 ELSE 0 END)                  AS cu_succeeded_count,
    SUM(CASE WHEN c.cu_status IN (N'Failed', N'Timeout') THEN 1 ELSE 0 END)      AS cu_failed_count,
    AVG(CAST(c.score AS FLOAT))                                                  AS avg_quality_score,
    SUM(c.error_count)                                                           AS total_errors,
    SUM(c.warning_count)                                                         AS total_warnings
FROM cu.PreProcessChecks c
GROUP BY CAST(c.checked_at AS DATE), c.usecase, c.analyzer_name;
GO
