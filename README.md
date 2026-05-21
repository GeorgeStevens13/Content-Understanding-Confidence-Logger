# Content Understanding Confidence Logger

Ingest Azure AI **Content Understanding** label/extraction JSON into **Azure SQL** so every
extracted field (and its confidence score) is queryable and reportable from **Power BI**.
Originals are moved from a `source` container to `processed` (or `failed`) once handled.

```mermaid
flowchart LR
    A[Blob Storage: source/<usecase>/<analyzer>/<file>.json] -->|Blob trigger| B[Azure Function App\nPython + Managed Identity]
    B -->|Parse + flatten fields| C[(Azure SQL: cu.Documents + cu.DocumentFields)]
    C --> D[Power BI\nReports on cu views]
    B -->|On success| E[Blob Storage: processed/<usecase>/<analyzer>/<file>.json]
    B -->|On failure| F[Blob Storage: failed/<usecase>/<analyzer>/<file>.json + .error.txt]
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

| View                       | Use                                                            |
| -------------------------- | -------------------------------------------------------------- |
| `cu.vw_DocumentFields`    | Flat fact table — one row per field. Main reporting surface.   |
| `cu.vw_DocumentSummary`   | One row per document — avg/min/max confidence, field count.    |
| `cu.vw_LowConfidenceFields`| Fields below 0.7 — review queue.                              |
| `cu.vw_FieldStatsByAnalyzer` | Per-analyzer / per-field-name confidence stats over time.    |

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

After that, drop a JSON file into:

```
<storage>/source/<usecase>/<analyzer>/<file>.json
```

and it will appear in `cu.Documents` + `cu.DocumentFields` within seconds, and
move to `<storage>/processed/<usecase>/<analyzer>/<file>.json`.

## Manual test checklist

Use this sequence to validate a new deployment quickly:

1. Upload a valid CU JSON file to `source/<usecase>/<analyzer>/<file>.json`.
2. Wait 5-30 seconds.
3. Confirm success path: blob moved to `processed/<usecase>/<analyzer>/<file>.json`.
4. Confirm failure path (if triggered): blob moved to `failed/<usecase>/<analyzer>/<file>.json` and `failed/<usecase>/<analyzer>/<file>.json.error.txt` exists.
5. Validate SQL rows in `cu.Documents` and `cu.DocumentFields`.

Common pitfall: uploading to `source/<file>.json` (missing `<usecase>/<analyzer>`) will not trigger ingestion.

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
    "LOW_CONFIDENCE_THRESHOLD": "0.70"
  }
}
EOF
# terminal B:
#   cd src && func start

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
    ├── function_app.py         # blob trigger
    ├── ingestion.py            # JSON -> rows (handles both CU formats, recursive)
    ├── sql_client.py           # pyodbc + MI
    ├── storage_client.py       # blob copy + delete (= move)
    ├── host.json
    ├── requirements.txt
    ├── local.settings.json.sample
    └── README.md
```
