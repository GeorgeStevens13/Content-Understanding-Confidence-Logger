-- =============================================================================
-- Content Understanding Confidence Logger — schema
-- =============================================================================
-- EAV-style design so any analyzer's field set works without schema changes.
-- Run AFTER you've created the SQL user for the Function's Managed Identity
-- (see sql/README.md). Idempotent.
-- =============================================================================

IF SCHEMA_ID('cu') IS NULL EXEC ('CREATE SCHEMA cu AUTHORIZATION dbo');
GO

-- ---------------------------------------------------------------------------
-- Documents — one row per ingested (blob_path, content_path) pair.
-- ---------------------------------------------------------------------------
IF OBJECT_ID('cu.Documents', 'U') IS NULL
BEGIN
    CREATE TABLE cu.Documents
    (
        document_id        BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Documents PRIMARY KEY,
        usecase            NVARCHAR(128)   NOT NULL,
        analyzer_name      NVARCHAR(256)   NOT NULL,            -- friendly, from blob path
        analyzer_id        NVARCHAR(256)   NULL,                -- result.analyzerId
        document_name      NVARCHAR(512)   NOT NULL,            -- filename / displayName
        blob_path          NVARCHAR(1024)  NOT NULL,            -- e.g. source/<usecase>/<analyzer>/<file>.json
        processed_blob_url NVARCHAR(2048)  NULL,                -- post-move URL
        content_path       NVARCHAR(256)   NOT NULL             -- analyze result content path ("input1"), or "labels" for labels file
                                              CONSTRAINT DF_Documents_ContentPath DEFAULT(N'input1'),
        mime_type          NVARCHAR(128)   NULL,
        source_created_at  DATETIMEOFFSET  NULL,                -- from result.createdAt or metadata.createdDateTime
        api_version        NVARCHAR(32)    NULL,                -- from result.apiVersion
        operation_id       NVARCHAR(64)    NULL,                -- top-level id
        status             NVARCHAR(32)    NULL,                -- top-level status (Succeeded, ...)
        field_count        INT             NOT NULL CONSTRAINT DF_Documents_FieldCount DEFAULT(0),
        avg_confidence     DECIMAL(5,4)    NULL,
        min_confidence     DECIMAL(5,4)    NULL,
        max_confidence     DECIMAL(5,4)    NULL,
        ingested_at        DATETIME2(3)    NOT NULL CONSTRAINT DF_Documents_IngestedAt DEFAULT(SYSUTCDATETIME()),
        CONSTRAINT UQ_Documents_BlobContent UNIQUE (blob_path, content_path)
    );

    CREATE INDEX IX_Documents_Usecase_Analyzer
        ON cu.Documents (usecase, analyzer_name)
        INCLUDE (document_name, ingested_at, avg_confidence);

    CREATE INDEX IX_Documents_IngestedAt
        ON cu.Documents (ingested_at DESC);
END
GO

-- ---------------------------------------------------------------------------
-- DocumentFields — one row per extracted LEAF field.
-- Nested objects/arrays are flattened into field_path:
--   CustomerAddress.City                 (object child)
--   LineItems[0].ItemDescription         (array element child)
--   LineItems[1].Price
-- ---------------------------------------------------------------------------
IF OBJECT_ID('cu.DocumentFields', 'U') IS NULL
BEGIN
    CREATE TABLE cu.DocumentFields
    (
        field_id       BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_DocumentFields PRIMARY KEY,
        document_id    BIGINT          NOT NULL,
        field_path     NVARCHAR(512)   NOT NULL,   -- full dotted/indexed path
        field_name     NVARCHAR(256)   NOT NULL,   -- leaf segment
        parent_path    NVARCHAR(512)   NULL,       -- everything before the leaf
        array_index    INT             NULL,       -- nearest enclosing array index (0..n) else NULL
        field_type     NVARCHAR(32)    NOT NULL,   -- string|number|date|time|integer|boolean|currency|address
        value_string   NVARCHAR(MAX)   NULL,       -- always populated (stringified value)
        value_number   FLOAT           NULL,
        value_integer  BIGINT          NULL,
        value_date     DATE            NULL,
        value_boolean  BIT             NULL,
        currency_code  NVARCHAR(8)     NULL,       -- for type=currency
        confidence     DECIMAL(5,4)    NULL,
        span_offset    INT             NULL,       -- first span only (most useful for joining back to text)
        span_length    INT             NULL,
        CONSTRAINT FK_DocumentFields_Documents
            FOREIGN KEY (document_id) REFERENCES cu.Documents(document_id) ON DELETE CASCADE
    );

    CREATE INDEX IX_DocumentFields_DocumentId
        ON cu.DocumentFields (document_id)
        INCLUDE (field_path, field_name, confidence, value_number);

    CREATE INDEX IX_DocumentFields_FieldName
        ON cu.DocumentFields (field_name)
        INCLUDE (confidence, value_number, document_id);

    CREATE INDEX IX_DocumentFields_Confidence
        ON cu.DocumentFields (confidence)
        WHERE confidence IS NOT NULL;
END
GO

-- ---------------------------------------------------------------------------
-- IngestionErrors — captures failures so they're queryable from Power BI too.
-- ---------------------------------------------------------------------------
IF OBJECT_ID('cu.IngestionErrors', 'U') IS NULL
BEGIN
    CREATE TABLE cu.IngestionErrors
    (
        error_id     BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_IngestionErrors PRIMARY KEY,
        blob_path    NVARCHAR(1024)  NOT NULL,
        usecase      NVARCHAR(128)   NULL,
        analyzer_name NVARCHAR(256)  NULL,
        error_kind   NVARCHAR(64)    NOT NULL,    -- ParseError|SqlError|MoveError|...
        error_message NVARCHAR(4000) NOT NULL,
        occurred_at  DATETIME2(3)    NOT NULL CONSTRAINT DF_IngestionErrors_OccurredAt DEFAULT(SYSUTCDATETIME())
    );

    CREATE INDEX IX_IngestionErrors_OccurredAt ON cu.IngestionErrors (occurred_at DESC);
END
GO

-- ---------------------------------------------------------------------------
-- Upsert procedure: replaces any prior rows for (blob_path, content_path),
-- inserts the Documents row, returns the new document_id.
-- Field rows are inserted by the app using a TVP-equivalent (executemany).
-- ---------------------------------------------------------------------------
IF OBJECT_ID('cu.usp_UpsertDocument', 'P') IS NOT NULL DROP PROCEDURE cu.usp_UpsertDocument;
GO
CREATE PROCEDURE cu.usp_UpsertDocument
    @usecase            NVARCHAR(128),
    @analyzer_name      NVARCHAR(256),
    @analyzer_id        NVARCHAR(256)   = NULL,
    @document_name      NVARCHAR(512),
    @blob_path          NVARCHAR(1024),
    @content_path       NVARCHAR(256)   = N'input1',
    @mime_type          NVARCHAR(128)   = NULL,
    @source_created_at  DATETIMEOFFSET  = NULL,
    @api_version        NVARCHAR(32)    = NULL,
    @operation_id       NVARCHAR(64)    = NULL,
    @status             NVARCHAR(32)    = NULL,
    @document_id        BIGINT OUTPUT
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    -- Replace any prior ingestion for this (blob, content). Cascades to fields.
    DELETE FROM cu.Documents
     WHERE blob_path = @blob_path AND content_path = @content_path;

    INSERT INTO cu.Documents
        (usecase, analyzer_name, analyzer_id, document_name, blob_path, content_path,
         mime_type, source_created_at, api_version, operation_id, status)
    VALUES
        (@usecase, @analyzer_name, @analyzer_id, @document_name, @blob_path, @content_path,
         @mime_type, @source_created_at, @api_version, @operation_id, @status);

    SET @document_id = SCOPE_IDENTITY();
END
GO

-- Update stats after field rows are inserted.
IF OBJECT_ID('cu.usp_FinalizeDocument', 'P') IS NOT NULL DROP PROCEDURE cu.usp_FinalizeDocument;
GO
CREATE PROCEDURE cu.usp_FinalizeDocument
    @document_id        BIGINT,
    @processed_blob_url NVARCHAR(2048) = NULL
AS
BEGIN
    SET NOCOUNT ON;

    UPDATE d
       SET d.field_count        = s.cnt,
           d.avg_confidence     = s.avg_conf,
           d.min_confidence     = s.min_conf,
           d.max_confidence     = s.max_conf,
           d.processed_blob_url = COALESCE(@processed_blob_url, d.processed_blob_url)
      FROM cu.Documents d
     CROSS APPLY (
        SELECT COUNT(*)        AS cnt,
               AVG(confidence) AS avg_conf,
               MIN(confidence) AS min_conf,
               MAX(confidence) AS max_conf
          FROM cu.DocumentFields f
         WHERE f.document_id = @document_id
     ) s
     WHERE d.document_id = @document_id;
END
GO
