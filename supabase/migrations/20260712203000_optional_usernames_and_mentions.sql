begin;

-- Public usernames are optional, unique handles. The legacy display_name was
-- seeded from OAuth metadata/email before handles became opt-in; column-level
-- grants below ensure that legacy private value is no longer browser-readable.
alter table public.profiles
  add column if not exists username text;

alter table public.profiles
  drop constraint if exists profiles_username_valid;
alter table public.profiles
  add constraint profiles_username_valid check (
    username is null or (
      username ~ '^[a-z0-9][a-z0-9_]{2,19}$'
      and username not in (
        'admin', 'administrator', 'dishtune', 'moderator',
        'official', 'staff', 'support'
      )
    )
  );

create unique index if not exists profiles_username_unique_ci
  on public.profiles (lower(username))
  where username is not null;

create index if not exists profiles_username_prefix_search
  on public.profiles (username text_pattern_ops)
  where username is not null;

drop policy if exists "own profile is editable" on public.profiles;
create policy "own profile is editable"
  on public.profiles for update
  using (auth.uid() = id)
  with check (auth.uid() = id);

-- New signups get a profile row, but no public identity until they choose it.
create or replace function public.handle_new_user()
returns trigger
language plpgsql security definer set search_path = public
as $$
begin
  insert into public.profiles (id, display_name, username)
  values (new.id, 'vegan explorer', null)
  on conflict (id) do nothing;
  return new;
end;
$$;

alter table public.comments
  add column if not exists user_mentions jsonb not null default '[]';

alter table public.comments
  drop constraint if exists comments_mentions_are_arrays;
alter table public.comments
  add constraint comments_mentions_are_arrays check (
    jsonb_typeof(mentions) = 'array'
    and jsonb_typeof(user_mentions) = 'array'
  );

alter table public.comments
  drop constraint if exists comments_user_mentions_shape;
alter table public.comments
  add constraint comments_user_mentions_shape check (
    jsonb_typeof(user_mentions) = 'array'
    and jsonb_array_length(user_mentions) <= 10
  );

create index if not exists comments_user_mentions_gin
  on public.comments using gin (user_mentions jsonb_path_ops);

create or replace function public.canonicalize_comment_user_mentions()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  raw_mention jsonb;
  mentioned_id uuid;
  actual_username text;
  canonical jsonb := '[]'::jsonb;
  seen_ids uuid[] := '{}'::uuid[];
begin
  if jsonb_typeof(new.user_mentions) is distinct from 'array' then
    raise exception using errcode = '22023', message = 'User mentions must be an array.';
  end if;
  if octet_length(new.user_mentions::text) > 4096
     or jsonb_array_length(new.user_mentions) > 10 then
    raise exception using errcode = '22023', message = 'Too many user mentions.';
  end if;

  for raw_mention in select value from jsonb_array_elements(new.user_mentions)
  loop
    if jsonb_typeof(raw_mention) <> 'object'
       or raw_mention ->> 'user_id' is null then
      raise exception using errcode = '22023', message = 'Invalid user mention.';
    end if;
    begin
      mentioned_id := (raw_mention ->> 'user_id')::uuid;
    exception when invalid_text_representation then
      raise exception using errcode = '22023', message = 'Invalid user mention.';
    end;

    if mentioned_id = any(seen_ids) then
      continue;
    end if;

    select p.username into actual_username
    from public.profiles p
    where p.id = mentioned_id and p.username is not null;
    if actual_username is null then
      raise exception using errcode = '22023', message = 'That username is no longer available.';
    end if;

    if not (new.body ~* (
      '(^|[^a-z0-9_])@' || actual_username || '([^a-z0-9_]|$)'
    )) then
      raise exception using errcode = '22023', message = 'A user mention is missing from the note.';
    end if;

    canonical := canonical || jsonb_build_array(jsonb_build_object(
      'user_id', mentioned_id::text,
      'username', actual_username
    ));
    seen_ids := array_append(seen_ids, mentioned_id);
  end loop;

  new.user_mentions := canonical;
  return new;
end;
$$;

drop trigger if exists canonicalize_comment_user_mentions on public.comments;
create trigger canonicalize_comment_user_mentions
  before insert on public.comments
  for each row execute function public.canonicalize_comment_user_mentions();

revoke all on table public.profiles from anon, authenticated;
grant select (id, username) on table public.profiles to anon, authenticated;
grant update (username) on table public.profiles to authenticated;
grant all on table public.profiles to service_role;

revoke execute on function public.handle_new_user()
  from public, anon, authenticated;
revoke execute on function public.canonicalize_comment_user_mentions()
  from public, anon, authenticated;

notify pgrst, 'reload schema';

commit;
