# Function app — local dev

```mermaid
flowchart TD
  A["Create local.settings.json from sample"] --> B["az login"]
  B --> C["cd src && func start"]
  C --> D["Upload one or more JSON files to<br/>source/&lt;usecase&gt;/&lt;analyzer&gt;/&lt;file&gt;.json"]
  D --> E["Wait for the next timer tick<br/>(INGEST_SCHEDULE, default 15 min)"]
  E --> L["Batch loop: up to BATCH_MAX_FILES blobs<br/>each under a 60s lease"]
  L --> F{Per-file result}
  F -->|Success| G["Blob moves to processed/…<br/>row in cu.Documents + cu.DocumentFields"]
  F -->|Failure| H["Blob moves to failed/…<br/>+ .error.txt sidecar<br/>+ row in cu.IngestionErrors"]
```

## Prerequisites

- Python 3.11 (the deployed app uses 3.11 — match locally to avoid surprises)
- Azure Functions Core Tools v4
- ODBC Driver 18 for SQL Server  
  Windows: <https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server>  
  Linux/WSL: `curl ... msodbcsql18` per the same doc

## Setup

```powershell
cd src
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item local.settings.json.sample local.settings.json
# Edit local.settings.json — fill in your storage account name and SQL server
az login                       # so DefaultAzureCredential can get tokens
func start
```

Drop one or more JSON files into
`<storage>/source/<usecase>/<analyzer>/<file>.json` and watch the logs. The
batch trigger runs on `INGEST_SCHEDULE` (default `0 */15 * * * *`); locally
you'll probably want a tighter schedule — set `INGEST_SCHEDULE` to
`*/30 * * * * *` in `local.settings.json` so it ticks every 30 seconds. After
ingestion, files land in `<storage>/processed/<usecase>/<analyzer>/<file>.json`
(or `failed/...` on error).

Important: the blob path must include both `<usecase>` and `<analyzer>`. A flat path like
`source/file.json` is silently skipped — the batch only picks up blobs that
match the three-segment layout.

## How it's wired

| File                | Purpose                                                                       |
| ------------------- | ----------------------------------------------------------------------------- |
| `function_app.py`   | Timer trigger + batch loop + failure handling + per-blob lease orchestration  |
| `ingestion.py`      | Parses both CU formats and flattens leaves into rows                          |
| `sql_client.py`     | Managed Identity → pyodbc connection; document + field writes; error logging |
| `storage_client.py` | List blobs + acquire short lease + server-side copy + delete (= move)         |
| `host.json`         | Functions host config (extension bundle, sampling, 10-min timeout)            |
| `requirements.txt`  | Python dependencies                                                           |

## Adding a new field type

`ingestion._VALUE_KEY` maps Content Understanding types to their `value<Type>`
key. To support a new type, add an entry there and a small branch in
`_build_leaf`. No SQL schema change is needed — typed columns are optional.
