-- Correct the outer object-name reference in the legacy Storage read policy.
-- This only affects files saved before paths included the owner's UUID.
drop policy if exists "legacy_blot_objects_select_own" on storage.objects;

create policy "legacy_blot_objects_select_own"
on storage.objects
for select
to authenticated
using (
  bucket_id = 'western-blots'
  and (storage.foldername(name))[1] = 'blots'
  and exists (
    select 1
    from public.blots b
    where b.id = (storage.foldername(storage.objects.name))[2]
      and b.owner_id = (select auth.uid())
  )
);
