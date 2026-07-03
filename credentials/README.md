# credentials/

This folder is for local GCP service account keys (Phase 6+).

**DO NOT commit any JSON files here to git** — they are already in `.gitignore`.

## Setup (Phase 6 — GCP Deploy)

1. Go to [GCP Console → IAM → Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts)
2. Create a service account with roles:
   - `Storage Object Admin` (for GCS file uploads)
   - `Cloud Run Invoker` (for service-to-service calls)
3. Download the JSON key
4. Save it here as `gcp-service-account.json`
5. Set in `.env`:
   ```
   GOOGLE_APPLICATION_CREDENTIALS=./credentials/gcp-service-account.json
   ```

## For now (Phases 1–5)

You don't need this file. `ENABLE_GCS_UPLOAD=false` in `.env` means files are saved locally.
