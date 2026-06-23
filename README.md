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
- `ALLOWED_ORIGINS=<your production origin>`
- `MAX_ZIP_UPLOAD_BYTES=262144000`

For production, set `frontend/config.js` to use same-origin API routes:

```js
BACKEND_URL: "/api",
USE_VERCEL_BLOB_UPLOADS: true,
```

The browser uploads large ZIPs directly to Vercel Blob, Flask processes them,
stores only temporary blot image objects, and the frontend calls cleanup when
blots are removed or the page closes.
