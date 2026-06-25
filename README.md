# WesternBlotAnalysis

Western blot quantification and fold-change analysis with a browser-session
workspace and a stateless Flask processing backend.

The app no longer has login, database persistence, or saved blot libraries. ZIP
uploads are processed into temporary file descriptors for the current browser
session. Scans, drawn boxes, and selected blots live only in memory and disappear
when the page is reloaded. Because this build has no durable per-user data store,
Supabase Auth and RLS are not required for the current security model.

## Local development

1. Copy `backend/.env.example` to `backend/.env` if you want to override defaults.
2. Start the Flask backend from `backend/` on port `5001`.
3. Serve `frontend/` with any static file server.
4. Keep `frontend/config.js` set to `BACKEND_URL: "http://127.0.0.1:5001"` and
   `USE_VERCEL_BLOB_UPLOADS: false`.

In local mode, temporary blot files are written under the OS temp directory unless
`BLOT_TEMP_DIR` is set.

## Vercel deployment

Deploy the repository root so Vercel sees `vercel.json`, `api/`, `frontend/`,
`requirements.txt`, and `package.json`.

Set these environment variables:

- `BLOT_TEMP_STORAGE=vercel-blob`
- `BLOB_READ_WRITE_TOKEN=<your Vercel Blob token>`
- `BLOB_ACCESS=public` if your Blob store is public, or omit it for a private
  Blob store
- `ALLOWED_ORIGINS=<your production origin>`
- `MAX_ZIP_UPLOAD_BYTES=262144000`
- Optional: `BLOB_UPLOAD_CALLBACK_ORIGIN=<your production origin>` if Blob
  callbacks should always use one canonical host

`frontend/config.js` automatically uses same-origin API routes and Vercel Blob
uploads outside localhost.

The browser uploads large ZIPs directly to Vercel Blob, Flask processes them,
stores only temporary blot image objects, and the frontend calls cleanup when
blots are removed or the page closes.
