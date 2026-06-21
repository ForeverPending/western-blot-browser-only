# Security Model

## Identity and authorization

- Supabase Auth is the identity provider. Flask accepts only an
  `Authorization: Bearer <access-token>` header and verifies it with the
  project's Auth service.
- Owner IDs are taken only from the verified token. Request bodies, paths, and
  query strings never choose an owner.
- Normal Postgres and Storage requests forward the user's token with the
  publishable key. Database and Storage RLS therefore remain active.
- A `401` means the token is missing, invalid, or expired. The frontend refreshes
  once and retries once. A `403` is never refreshed or retried.

## Session lifecycle

- Authenticated browser sessions are signed out after 30 minutes without user
  activity. Configure the duration with `INACTIVITY_TIMEOUT_MINUTES` in
  `frontend/config.js`.
- Activity is coordinated across same-user tabs without sharing blot data, so
  an idle background tab does not end an actively used session.
- Manual and inactivity sign-out clear account-derived UI state, sign out of
  Supabase, and reload the page before another user can enter the workspace.

## Service-role key policy

- The service-role key may exist only in a backend or worker environment.
- It must never appear in frontend code, URLs, logs, browser responses, preview
  deployment variables, or client-visible configuration.
- It is reserved for verified Stripe webhooks, controlled background workers,
  migrations, and narrowly scoped administration.
- It must not be used for ordinary user CRUD or any endpoint that accepts an
  arbitrary object path, owner ID, table name, or operation from a client.
- Privileged workers obtain ownership from an already-authorized database job,
  not from user-supplied metadata. Rotate the key after suspected exposure.

## Schema discipline

- New public tables must be created by a reviewed migration with RLS and FORCE
  RLS enabled, least-privilege grants, and explicit policies before application
  code references them.
- Keep Supabase's automatic RLS setting enabled for new public tables.
- Run `backend/rls_audit.sql` after each migration. CI should test anonymous,
  owner, and different-user CRUD denial cases.
- FORCE RLS does not constrain the service role, which has BYPASSRLS.

## Upload boundary

- Browsers upload ZIPs directly to the private `western-blots` bucket under
  `<user-id>/uploads/<random-id>.zip` using resumable TUS uploads.
- Storage RLS validates the first path component. Flask also checks the path
  against the verified user before downloading it with that user's token.
- Uploaded files remain untrusted. The processor checks archive paths,
  encryption, duplicate names, compression ratio, decoded size, entry count,
  TIFF type, page count, dimensions, and pixel count before decoding.
- Temporary ZIPs are deleted after processing. Approved TIFF/JPEG outputs are
  written under `<user-id>/blots/<blot-id>/`.

## Deployment

- Set `ALLOWED_ORIGINS` to exact production and intentionally supported preview
  origins. CORS is not an authentication control.
- Configure Vercel WAF rate limiting for API and authentication routes. Use a
  shared rate-limit store when Flask runs on more than one process.
- Keep production, preview, and development secrets separate. Preview builds
  should not receive the production service-role key.
- Keep the Storage bucket private and the Supabase Auth redirect allow-list as
  narrow as possible.
