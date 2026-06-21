-- The RLS event-trigger helper must be executable by PostgreSQL itself, but it
-- must not be exposed as a client-callable Data API RPC.
revoke execute on function public.rls_auto_enable() from PUBLIC;
revoke execute on function public.rls_auto_enable() from anon;
revoke execute on function public.rls_auto_enable() from authenticated;
revoke execute on function public.rls_auto_enable() from service_role;

-- Make future functions private by default. Grant EXECUTE explicitly if a
-- function is intentionally designed to be called through the Data API.
alter default privileges for role postgres in schema public
  revoke execute on functions from PUBLIC;
