-- Upgrade the original single-owner schema to Supabase Auth ownership.
-- Create or invite your first Supabase Auth user before running this file.
-- If more than one Auth user already exists, this migration deliberately stops
-- rather than guessing who owns the legacy data.

begin;

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text check (char_length(display_name) <= 200),
  created_at timestamptz not null default now()
);

alter table public.blots add column if not exists owner_uuid uuid references auth.users(id) on delete cascade;
alter table public.scans add column if not exists owner_uuid uuid references auth.users(id) on delete cascade;

do $$
declare
  auth_user_count integer;
  legacy_owner uuid;
begin
  select count(*) into auth_user_count from auth.users;
  select id into legacy_owner from auth.users order by created_at limit 1;
  if exists (select 1 from public.blots where owner_uuid is null) then
    if auth_user_count <> 1 then
      raise exception 'Legacy data needs exactly one Auth user for automatic ownership assignment; found %', auth_user_count;
    end if;
    update public.blots set owner_uuid = legacy_owner where owner_uuid is null;
  end if;
  update public.scans s
    set owner_uuid = b.owner_uuid
    from public.blots b
    where s.blot_id = b.id and s.owner_uuid is null;
end $$;

alter table public.blots alter column owner_uuid set not null;
alter table public.scans alter column owner_uuid set not null;
alter table public.blots drop column if exists owner_id;
alter table public.scans drop column if exists owner_id;
alter table public.blots rename column owner_uuid to owner_id;
alter table public.scans rename column owner_uuid to owner_id;

alter table public.blots alter column owner_id set default auth.uid();
alter table public.scans alter column owner_id set default auth.uid();
alter table public.blots add constraint blots_id_owner_unique unique (id, owner_id);
alter table public.scans drop constraint if exists scans_blot_id_fkey;
alter table public.scans add constraint scans_blot_owner_fkey
  foreign key (blot_id, owner_id) references public.blots(id, owner_id) on delete cascade;

alter table public.blots add constraint blots_name_length check (char_length(name) between 1 and 200);
alter table public.scans add constraint scans_protein_name_length check (char_length(protein_name) between 1 and 200);

create table if not exists public.billing_accounts (
  owner_id uuid primary key references auth.users(id) on delete cascade,
  stripe_customer_id text unique,
  stripe_subscription_id text unique,
  subscription_status text,
  price_id text,
  current_period_end timestamptz,
  updated_at timestamptz not null default now()
);

create index if not exists idx_blots_owner_id on public.blots(owner_id);
create index if not exists idx_scans_owner_id on public.scans(owner_id);

alter table public.profiles enable row level security;
alter table public.blots enable row level security;
alter table public.scans enable row level security;
alter table public.billing_accounts enable row level security;
alter table public.profiles force row level security;
alter table public.blots force row level security;
alter table public.scans force row level security;
alter table public.billing_accounts force row level security;

revoke all on public.profiles, public.blots, public.scans, public.billing_accounts from anon;
grant usage on schema public to authenticated, service_role;
grant select, insert, update on public.profiles to authenticated;
grant select, insert, update, delete on public.blots, public.scans to authenticated;
grant select on public.billing_accounts to authenticated;
grant usage, select on sequence public.scans_id_seq to authenticated;
grant select, insert, update, delete on public.profiles, public.blots, public.scans, public.billing_accounts to service_role;
grant usage, select on sequence public.scans_id_seq to service_role;

drop policy if exists "profiles_select_own" on public.profiles;
drop policy if exists "profiles_insert_own" on public.profiles;
drop policy if exists "profiles_update_own" on public.profiles;
drop policy if exists "blots_select_own" on public.blots;
drop policy if exists "blots_insert_own" on public.blots;
drop policy if exists "blots_update_own" on public.blots;
drop policy if exists "blots_delete_own" on public.blots;
drop policy if exists "scans_select_own" on public.scans;
drop policy if exists "scans_insert_own" on public.scans;
drop policy if exists "scans_update_own" on public.scans;
drop policy if exists "scans_delete_own" on public.scans;
drop policy if exists "billing_select_own" on public.billing_accounts;

create policy "profiles_select_own" on public.profiles for select to authenticated using ((select auth.uid()) = id);
create policy "profiles_insert_own" on public.profiles for insert to authenticated with check ((select auth.uid()) = id);
create policy "profiles_update_own" on public.profiles for update to authenticated using ((select auth.uid()) = id) with check ((select auth.uid()) = id);
create policy "blots_select_own" on public.blots for select to authenticated using ((select auth.uid()) = owner_id);
create policy "blots_insert_own" on public.blots for insert to authenticated with check ((select auth.uid()) = owner_id);
create policy "blots_update_own" on public.blots for update to authenticated using ((select auth.uid()) = owner_id) with check ((select auth.uid()) = owner_id);
create policy "blots_delete_own" on public.blots for delete to authenticated using ((select auth.uid()) = owner_id);
create policy "scans_select_own" on public.scans for select to authenticated using ((select auth.uid()) = owner_id);
create policy "scans_insert_own" on public.scans for insert to authenticated with check ((select auth.uid()) = owner_id);
create policy "scans_update_own" on public.scans for update to authenticated using ((select auth.uid()) = owner_id) with check ((select auth.uid()) = owner_id);
create policy "scans_delete_own" on public.scans for delete to authenticated using ((select auth.uid()) = owner_id);
create policy "billing_select_own" on public.billing_accounts for select to authenticated using ((select auth.uid()) = owner_id);

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('western-blots', 'western-blots', false, 262144000,
  array['application/zip', 'application/x-zip-compressed', 'image/tiff', 'image/jpeg'])
on conflict (id) do update set public = false,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "blot_objects_select_own" on storage.objects;
drop policy if exists "blot_objects_insert_own" on storage.objects;
drop policy if exists "blot_objects_update_own" on storage.objects;
drop policy if exists "blot_objects_delete_own" on storage.objects;
drop policy if exists "legacy_blot_objects_select_own" on storage.objects;

create policy "blot_objects_select_own" on storage.objects for select to authenticated
  using (bucket_id = 'western-blots' and (storage.foldername(name))[1] = (select auth.uid())::text);
create policy "blot_objects_insert_own" on storage.objects for insert to authenticated
  with check (bucket_id = 'western-blots' and (storage.foldername(name))[1] = (select auth.uid())::text);
create policy "blot_objects_update_own" on storage.objects for update to authenticated
  using (bucket_id = 'western-blots' and (storage.foldername(name))[1] = (select auth.uid())::text)
  with check (bucket_id = 'western-blots' and (storage.foldername(name))[1] = (select auth.uid())::text);
create policy "blot_objects_delete_own" on storage.objects for delete to authenticated
  using (bucket_id = 'western-blots' and (storage.foldername(name))[1] = (select auth.uid())::text);

-- Temporary compatibility for files written before paths included owner UUIDs.
create policy "legacy_blot_objects_select_own" on storage.objects for select to authenticated
  using (
    bucket_id = 'western-blots'
    and (storage.foldername(name))[1] = 'blots'
    and exists (
      select 1 from public.blots b
      where b.id = (storage.foldername(storage.objects.name))[2]
        and b.owner_id = (select auth.uid())
    )
  );

commit;
