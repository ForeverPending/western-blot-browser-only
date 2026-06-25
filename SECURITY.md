# Security Model

## Browser-session workspace

- The app has no login system and no per-user database records.
- Supabase Auth, database RLS, and per-user Storage policies are intentionally
  not part of this build because there is no durable per-user data store.
- A random browser-session id is stored in `sessionStorage` and used only to
  namespace temporary upload paths.
- Blots and scans are not durable application data. Reloading the page clears the
  visible workspace; temporary backend objects are cleaned up on blot deletion or
  page close when the browser can send the cleanup request.

## Authorization stance

- Public API routes are expected for this anonymous, stateless workflow. They do
  not grant access to saved accounts, saved blots, or Supabase rows because those
  resources do not exist in this build.
- The security boundary is the temporary browser-session namespace. Treat the
  session id as an unguessable capability for temporary objects, not as a user
  identity.
- If the product later adds accounts, saved blot libraries, shared projects,
  billing-linked state, or durable history, add authenticated authorization and
  database/storage RLS before storing that data.

## Upload boundary

- Uploaded ZIPs are untrusted. The processor checks archive paths, encryption,
  duplicate names, compression ratio, decoded size, entry count, TIFF type, page
  count, dimensions, and pixel count before decoding.
- Vercel Blob descriptors are untrusted. The backend validates that descriptor
  paths stay in the active session namespace and that Blob URLs point to the
  matching Vercel Blob object before reading or deleting.
- Local development stores temporary blot files under `BLOT_TEMP_DIR` or the OS
  temp directory.
- Vercel production mode should use Vercel Blob. The browser uploads ZIPs under
  `uploads/<session-id>/<random-id>.zip`; processed images are written under
  `sessions/<session-id>/<blot-id>/`.
- Backend endpoints reject temporary file descriptors outside the active
  session namespace.

## No durable storage guarantee

- This is intentionally not a storage product. Do not rely on the app to preserve
  blots, scans, or results after reload, tab close, failed cleanup, deployment, or
  provider lifecycle events.
- Temporary Blob objects may remain if a browser closes before cleanup completes.
  Use provider lifecycle/retention tooling if production needs guaranteed
  expiration.

## Deployment

- Set `ALLOWED_ORIGINS` to exact production and intentionally supported preview
  origins. CORS is not an authorization layer.
- Set `BLOB_UPLOAD_CALLBACK_ORIGIN` when Blob upload callbacks should be pinned
  to one canonical deployment host instead of the matching allowed origin.
- Set `BLOT_TEMP_STORAGE=vercel-blob` and keep `BLOB_READ_WRITE_TOKEN` only in
  backend/Vercel environment variables.
- Do not put Blob tokens, backend secrets, or private object URLs in
  `frontend/config.js`.
- Keep `MAX_ZIP_BYTES`, `MAX_ZIP_UPLOAD_BYTES`, and
  `BLOB_UPLOAD_TOKEN_RATE_LIMIT` aligned with your hosting memory and cost
  limits.
- Configure Vercel WAF or platform rate limiting for public API routes. The
  in-app rate limits are useful guardrails, not a replacement for edge-level
  abuse controls.
