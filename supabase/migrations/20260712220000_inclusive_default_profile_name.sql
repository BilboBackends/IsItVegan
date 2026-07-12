begin;

-- The private legacy display_name is no longer exposed by the public app,
-- but keep its fallback inclusive and consistent with the UI's public label.
alter table public.profiles
  alter column display_name set default 'dish explorer';

update public.profiles
set display_name = 'dish explorer'
where display_name = 'vegan explorer';

create or replace function public.handle_new_user()
returns trigger
language plpgsql security definer set search_path = public
as $$
begin
  insert into public.profiles (id, display_name, username)
  values (new.id, 'dish explorer', null)
  on conflict (id) do nothing;
  return new;
end;
$$;

revoke execute on function public.handle_new_user()
  from public, anon, authenticated;

notify pgrst, 'reload schema';

commit;
