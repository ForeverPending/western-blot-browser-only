# WesternBlotAnalysis

Western blot quantification and fold-change analysis with Supabase Auth,
Postgres Row Level Security, and private per-user Storage paths.

## Multi-user setup

1. Create or invite the initial user in Supabase Auth.
2. For an existing database, run `backend/supabase_multi_user_migration.sql` in
   the SQL editor. For a new database, run `backend/supabase_schema.sql`.
3. Copy `backend/.env.example` to `backend/.env` and set the Supabase URL,
   publishable key, exact frontend origins, and Storage backend.
4. Put the same public Supabase URL and publishable key in `frontend/config.js`.
5. Disable public signups in Supabase and invite the intended users.
6. Run `backend/rls_audit.sql` after schema changes and test with two accounts.

The Supabase service-role key is not used by ordinary API requests. See
`SECURITY.md` before adding background jobs, billing, or administrative routes.
