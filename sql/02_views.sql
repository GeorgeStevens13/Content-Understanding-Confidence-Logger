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
