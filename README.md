# WesternBlotAnalysis

Western blot quantification and fold-change analysis with a browser-session
workspace and a stateless Flask processing backend.

The app has no login, database persistence, or saved blot libraries. ZIP
uploads are processed into temporary file descriptors for the current browser
session. Loaded blots and saved scans are mirrored to `sessionStorage` so a
same-tab reload can restore the workspace, but they are not durable server-side
records and are cleared when the browser session ends or the user clears the
workspace. Because there is no durable per-user data store, authentication and
database access controls are not part of the security model.

## Local development

1. Copy `backend/.env.example` to `backend/.env` if you want to override defaults.
2. Start the Flask backend from `backend/` on port `5001`.
3. Serve `frontend/` with any static file server.
4. No `frontend/config.js` changes are needed. When served from `localhost` or
   `127.0.0.1`, it auto-detects local mode: `BACKEND_URL` becomes
   `http://127.0.0.1:5001` and `USE_VERCEL_BLOB_UPLOADS` is disabled.

In local mode, temporary blot files are written under the OS temp directory unless
`BLOT_TEMP_DIR` is set. The UI reads the backend's effective direct-upload limit,
so it will not offer a ZIP larger than Flask can accept.

## Vercel deployment

Deploy the repository root so Vercel sees `vercel.json`, `api/`, `frontend/`,
`requirements.txt`, and `package.json`.

Set these environment variables:

- `BLOT_TEMP_STORAGE=vercel-blob`
- `BLOB_READ_WRITE_TOKEN=<your Vercel Blob token>`
- `BLOB_ACCESS=public` and `BLOB_PUBLIC_ACCESS_ACK=true` if your Blob store is
  public, or omit both for a private Blob store
- `ALLOWED_ORIGINS=<your production origin>`
- `MAX_ZIP_UPLOAD_BYTES=262144000`
- `RATELIMIT_STORAGE_URI=<shared Redis/Upstash URI>`
- `CRON_SECRET=<random string of at least 16 characters>`
- Optional: `BLOB_UPLOAD_CALLBACK_ORIGIN=<your production origin>` if Blob
  callbacks should always use one canonical host

`frontend/config.js` automatically uses same-origin API routes and Vercel Blob
uploads outside localhost.

The browser uploads large ZIPs directly to Vercel Blob. Flask streams each ZIP
through a temporary file and validates/stores one image member at a time. The
frontend calls cleanup when blots are removed or the workspace is explicitly
cleared; a protected daily Vercel Cron reclaims abandoned session objects.

Production startup rejects in-memory Flask rate limiting because serverless
instances do not share it. Set `ALLOW_IN_MEMORY_RATE_LIMITS=true` only when an
equivalent Vercel WAF/edge policy is already enforcing request and byte quotas.
The Blob-token endpoint also keeps a small per-instance guardrail, but edge
limits remain necessary because that endpoint is intentionally anonymous.

Spreadsheet parsing is served from the pinned local
`frontend/vendor/xlsx.full.min.js` bundle, so the deployed app does not depend on
third-party scripts or fonts at runtime.

## Validation

- `npm run check`
- `./venv/bin/python -m unittest backend/test_security.py`
