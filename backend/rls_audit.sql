-- Run after each schema change. This returns one combined result set so the
-- Supabase SQL Editor shows tables, policies, and bucket configuration together.
with security_audit as (
  select
    1 as sort_order,
    'table'::text as object_type,
    c.relname::text as object_name,
    jsonb_build_object(
      'rls', c.relrowsecurity,
      'force_rls', c.relforcerowsecurity
    ) as details
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relkind = 'r'

  union all

  select
    2,
    'policy',
    schemaname || '.' || tablename || '.' || policyname,
    jsonb_build_object(
      'roles', roles,
      'command', cmd,
      'using', qual,
      'with_check', with_check
    )
  from pg_policies
  where schemaname in ('public', 'storage')

  union all

  select
    3,
    'bucket',
    id,
    jsonb_build_object(
      'public', public,
      'file_size_limit', file_size_limit,
      'allowed_mime_types', allowed_mime_types
    )
  from storage.buckets
  where id = 'western-blots'
)
select object_type, object_name, details
from security_audit
order by sort_order, object_type, object_name;
