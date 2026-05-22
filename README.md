# Content Understanding Confidence Logger

Ingest Azure AI **Content Understanding** label/extraction JSON into **Azure SQL** so every
extracted field (and its confidence score) is queryable and reportable from **Power BI**.
Originals are moved from a `source` container to `processed` (or `failed`) once handled.

Processing is **timer-driven**: every 15 minutes (configurable via `INGEST_SCHEDULE`)
the Function App scans the `source` container and processes up to `BATCH_MAX_FILES`
blobs in a single batch.

```mermaid
flowchart LR
    T["Timer<br/>(INGEST_SCHEDULE, default every 15 min UTC)"] --> B
    A["Blob Storage<br/>source/&lt;usecase&gt;/&lt;analyzer&gt;/&lt;file&gt;.json"] -->|List + 60s lease| B
    B["Azure Function App<br/>Python 3.11 · Managed Identity<br/>batch up to BATCH_MAX_FILES per tick"]
    B -->|Parse + flatten fields<br/>upsert via cu.usp_UpsertDocument| C[("Azure SQL<br/>cu.Documents · cu.DocumentFields")]
    C --> D["Power BI<br/>cu.vw_* views"]
    B -->|On success| E["processed/&lt;usecase&gt;/&lt;analyzer&gt;/&lt;file&gt;.json"]
    B -->|On failure| F["failed/&lt;usecase&gt;/&lt;analyzer&gt;/&lt;file&gt;.json<br/>+ .error.txt sidecar<br/>+ row in cu.IngestionErrors"]
```

## What gets stored

For every JSON file ingested:

| Table              | Purpose                                                                 |
| ------------------ | ----------------------------------------------------------------------- |
| `cu.Documents`     | One row per document — usecase, analyzer, filename, blob path, mime, …  |
| `cu.DocumentFields` | One row per extracted field — field name, value, **confidence**, spans |

Re-ingesting the same blob path **upserts** (replaces) — safe to replay.

## Blob naming convention

The usecase and analyzer come from the blob path:

```
source/<usecase>/<analyzer>/<file>.json
       └─ invoices                    -> usecase
                  └─ contoso-invoice-v3 -> analyzer
                                         └─ file.json -> document_name
```

The `displayName` in the JSON `metadata` block is used as the document name when present;
otherwise the filename is used.

## Power BI

Connect Power BI Desktop → **Azure SQL Database** → enter the server name & DB →
authenticate (Microsoft account / Entra ID). Pick from these views:

| View                         | Use                                                            |
| ---------------------------- | -------------------------------------------------------------- |
| `cu.vw_DocumentFields`       | Flat fact table — one row per field. Main reporting surface.   |
| `cu.vw_DocumentSummary`      | One row per document — avg/min/max confidence, field count.    |
| `cu.vw_LowConfidenceFields`  | Fields below `LOW_CONFIDENCE_THRESHOLD` (default 0.7).         |
| `cu.vw_FieldStatsByAnalyzer` | Per-analyzer / per-field-name confidence stats over time.      |
| `cu.vw_DailyIngestion`       | Daily volume + average confidence + error count.               |

## Deploy

```powershell
# from repo root
azd auth login
azd init -e dev                       # only the first time
azd env set AZURE_LOCATION australiaeast   # pick any region with Azure SQL + Functions
azd up
```

`azd up` will:

1. Provision storage (with `source`, `processed`, `failed` containers), Azure SQL
   (Entra-only auth, **you** become the SQL admin), App Insights, and a Linux
   Consumption Python Function App.
2. Grant the Function App's Managed Identity `Storage Blob Data Owner`,
   `Storage Queue Data Contributor`, and `Storage Table Data Contributor`
   on the storage account (required for identity-based `AzureWebJobsStorage`).
3. Build and deploy the Python function code.

### Python package gotcha (Linux Y1 Consumption)

`azd deploy` for Linux Consumption Python does **not** package wheels — it
uploads source only and relies on Oryx remote build, which isn't always reliable
on Y1. If `az functionapp function list -g <rg> -n <func>` returns 0 after
deploy, build and stage the package manually:

```bash
# from repo root (Linux/WSL recommended for matching wheels)
rm -rf /tmp/funcpkg && mkdir /tmp/funcpkg && cp -r src/* /tmp/funcpkg/
cd /tmp/funcpkg
pip install --target ./.python_packages/lib/site-packages \
  --platform manylinux_2_17_x86_64 --python-version 3.11 \
  --only-binary=:all: --implementation cp -r requirements.txt
zip -r -q /tmp/funcpkg.zip .

# upload + point the Function App at it (MI-based)
SA=stcucdevXXXXXXXX   # your storage account
FUNC=func-cuc-dev-XXXXXXXX
RG=rg-dev
az storage container create --account-name $SA --name app-package --auth-mode login
az storage blob upload --account-name $SA --container-name app-package \
  --name funcpkg.zip --file /tmp/funcpkg.zip --auth-mode login --overwrite
az functionapp config appsettings set -g $RG -n $FUNC --settings \
  WEBSITE_RUN_FROM_PACKAGE=https://$SA.blob.core.windows.net/app-package/funcpkg.zip \
  WEBSITE_RUN_FROM_PACKAGE__credential=managedidentity \
  SCM_DO_BUILD_DURING_DEPLOYMENT=false ENABLE_ORYX_BUILD=false
az functionapp restart -g $RG -n $FUNC
```

### One-time post-deploy steps

Two SQL steps are needed once (Azure can't fully automate Entra DB users via Bicep):

1. Grant the Function App's Managed Identity access to the DB
2. Create the schema and views

Full T-SQL and instructions: [sql/README.md](sql/README.md).

After that, drop one or more JSON files into:

```
<storage>/source/<usecase>/<analyzer>/<file>.json
```

They will be picked up on the next 15-minute timer tick (or sooner if you set
`INGEST_SCHEDULE` to a tighter NCRONTAB expression), inserted into
`cu.Documents` + `cu.DocumentFields`, and moved to
`<storage>/processed/<usecase>/<analyzer>/<file>.json`.

### Ingest schedule

| Setting                  | Default          | Notes                                                                                  |
| ------------------------ | ---------------- | -------------------------------------------------------------------------------------- |
| `INGEST_SCHEDULE`        | `0 */15 * * * *` | NCRONTAB (UTC). Top of the hour + every 15 min. Use `0 */5 * * * *` for 5-min cadence. |
| `BATCH_MAX_FILES`        | `50`             | Max blobs processed per tick. Anything not consumed waits for the next tick.           |
| `BATCH_TIME_BUDGET_SEC`  | `540`            | Soft cutoff (9 min) so the loop never bumps into the 10-min host timeout.              |

Change them with `az functionapp config appsettings set` (no redeploy needed).

## Manual test checklist

Use this sequence to validate a new deployment quickly:

1. Upload one or more valid CU JSON files to `source/<usecase>/<analyzer>/<file>.json`.
2. Wait for the next timer tick (up to 15 minutes with the default schedule),
   or invoke the function on demand. Two options:

   **Option A — admin endpoint (fast path used in our E2E test):**
   ```bash
   FUNC=func-cuc-dev-xxxxxxxx
   RG=rg-dev
   MASTER_KEY=$(az functionapp keys list -g $RG -n $FUNC --query masterKey -o tsv)
   curl -s -o /dev/null -w "%{http_code}\n" -X POST \
     -H "x-functions-key: $MASTER_KEY" -H "Content-Type: application/json" \
     -d '{"input":""}' \
     "https://$FUNC.azurewebsites.net/admin/functions/ingest_content_understanding_batch"
   # → 202 means accepted; the timer runs the batch loop asynchronously.
   ```

   **Option B — ARM `az rest`:**
   ```bash
   az rest --method post \
     --uri "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.Web/sites/$FUNC/functions/ingest_content_understanding_batch/invoke?api-version=2024-04-01" \
     --body '{}'
   ```

   Or temporarily tighten the schedule: `az functionapp config appsettings set -g $RG -n $FUNC --settings INGEST_SCHEDULE="*/30 * * * * *"` (every 30 s).

3. Confirm success path: blobs moved to `processed/<usecase>/<analyzer>/<file>.json`.
4. Confirm failure path (if triggered): blob moved to `failed/<usecase>/<analyzer>/<file>.json`, sidecar `failed/<usecase>/<analyzer>/<file>.json.error.txt` exists, **and** a row in `cu.IngestionErrors` is created.
5. Validate SQL rows. The `blob_path` column stores the **source-relative** path, even after the blob is moved to `processed/`:
   ```sql
   SELECT document_id, usecase, analyzer_name, document_name, status,
          field_count, avg_confidence, processed_blob_url, ingested_at
   FROM cu.Documents
   WHERE blob_path LIKE 'source/<usecase>/<analyzer>/%'
   ORDER BY ingested_at DESC;

   SELECT TOP 20 d.document_name, f.field_path, f.field_type,
                 f.value_string, f.value_number, f.confidence
   FROM cu.Documents d
   JOIN cu.DocumentFields f ON f.document_id = d.document_id
   WHERE d.blob_path LIKE 'source/<usecase>/<analyzer>/%'
   ORDER BY d.document_id, f.field_path;
   ```

Common pitfalls:
- Uploading to `source/<file>.json` (missing `<usecase>/<analyzer>`) is silently skipped — only blobs that match the three-segment layout are picked up.
- All five failed files in one tick? Check `cu.IngestionErrors.error_message` first — the common cause is SQL `publicNetworkAccess` drifting back to `Disabled` (see [sql/README.md](sql/README.md#network-requirement)).

### Known-good one-shot E2E (WSL/Azure CLI)

This is a copy/paste flow that was validated end-to-end with the production
analyze-result shape (`result.contents`):

```bash
# from repo root
set -e
set -a && . .azure/dev/.env && set +a

# 1) Start local host (identity-based) in a separate terminal
cat > src/local.settings.json <<EOF
{
  "IsEncrypted": false,
  "Values": {
    "FUNCTIONS_WORKER_RUNTIME": "python",
    "AzureWebJobsFeatureFlags": "EnableWorkerIndexing",
    "AzureWebJobsStorage__accountName": "$STORAGE_ACCOUNT_NAME",
    "AzureWebJobsStorage__blobServiceUri": "https://$STORAGE_ACCOUNT_NAME.blob.core.windows.net/",
    "AzureWebJobsStorage__queueServiceUri": "https://$STORAGE_ACCOUNT_NAME.queue.core.windows.net/",
    "AzureWebJobsStorage__tableServiceUri": "https://$STORAGE_ACCOUNT_NAME.table.core.windows.net/",
    "AzureWebJobsStorage__credential": "AzureCli",
    "SOURCE_CONTAINER": "source",
    "PROCESSED_CONTAINER": "processed",
    "FAILED_CONTAINER": "failed",
    "SQL_SERVER": "$SQL_SERVER",
    "SQL_DATABASE": "$SQL_DATABASE",
    "LOW_CONFIDENCE_THRESHOLD": "0.70",
    "INGEST_SCHEDULE": "*/30 * * * * *",
    "BATCH_MAX_FILES": "20",
    "BATCH_TIME_BUDGET_SEC": "60"
  }
}
EOF
# terminal B:
#   cd src && func start
# (locally we use a 30-second NCRONTAB so you don't wait 15 minutes between
#  uploads and verification)

# 2) Upload probe JSON (production analyze-result shape)
TS=$(date +%Y%m%d%H%M%S)
BLOB="e2e/local/cu-e2e-analyze-$TS.json"
cat > /tmp/cu-e2e-analyze-$TS.json <<EOF
{
  "id": "op-$TS",
  "status": "succeeded",
  "result": {
    "analyzerId": "invoice-analyzer-v1",
    "apiVersion": "2024-11-30",
    "createdAt": "2026-05-20T11:08:00Z",
    "contents": [
      {
        "path": "input1",
        "fields": {
          "InvoiceId": {"type": "string", "valueString": "A-100", "confidence": 0.99},
          "Total": {"type": "number", "valueNumber": 123.45, "confidence": 0.97}
        }
      }
    ]
  }
}
EOF
az storage blob upload --auth-mode login --account-name "$STORAGE_ACCOUNT_NAME" \
  -c source -n "$BLOB" -f /tmp/cu-e2e-analyze-$TS.json --overwrite -o none
echo "Uploaded: source/$BLOB"

# 3) Verify storage movement
SRC_EXISTS=$(az storage blob exists --auth-mode login --account-name "$STORAGE_ACCOUNT_NAME" -c source -n "$BLOB" --query exists -o tsv)
PROCESSED_EXISTS=$(az storage blob exists --auth-mode login --account-name "$STORAGE_ACCOUNT_NAME" -c processed -n "$BLOB" --query exists -o tsv)
FAILED_EXISTS=$(az storage blob exists --auth-mode login --account-name "$STORAGE_ACCOUNT_NAME" -c failed -n "$BLOB" --query exists -o tsv)
echo "source=$SRC_EXISTS"
echo "processed=$PROCESSED_EXISTS"
echo "failed=$FAILED_EXISTS"

# 4) Verify SQL rows
sqlcmd -S "tcp:$SQL_SERVER,1433" -d "$SQL_DATABASE" -G -N -Q "
SET NOCOUNT ON;
SELECT TOP 1 d.document_id, d.usecase, d.analyzer_name, d.content_path, d.field_count, d.ingested_at
FROM cu.Documents d
WHERE d.blob_path = 'source/$BLOB'
ORDER BY d.ingested_at DESC;

SELECT COUNT(*) AS field_rows
FROM cu.DocumentFields f
INNER JOIN cu.Documents d ON d.document_id = f.document_id
WHERE d.blob_path = 'source/$BLOB';"
```

Expected outcome:

- `source=false`
- `processed=true`
- `failed=false`
- one `cu.Documents` row for `source/$BLOB` and `field_rows > 0`

## Local dev

See [src/README.md](src/README.md).

## Layout

```
.
├── azure.yaml                  # azd project manifest
├── infra/
│   ├── main.bicep              # entry — RG-scope
│   ├── main.parameters.json
│   └── modules/
│       ├── storage.bicep
│       ├── sql.bicep
│       ├── function.bicep
│       └── monitoring.bicep
├── sql/
│   ├── 01_schema.sql           # tables + upsert procs
│   ├── 02_views.sql            # Power BI views
│   └── README.md               # post-deploy SQL steps
└── src/
    ├── function_app.py         # timer trigger + batch loop + failure handling
    ├── ingestion.py            # JSON -> rows (handles both CU formats, recursive)
    ├── sql_client.py           # pyodbc + MI; usp_UpsertDocument / usp_FinalizeDocument / log_error
    ├── storage_client.py       # list + 60s lease + server-side copy + delete (= move)
    ├── host.json               # functionTimeout 00:10:00 (Y1 max)
    ├── requirements.txt
    ├── local.settings.json.sample
    └── README.md
```
