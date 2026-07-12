-- VeganFind user-data plane (Supabase Postgres).
--
-- The public site stays a static GitHub Pages build; the browser talks to
-- Supabase directly with the publishable anon key. EVERY guarantee therefore
-- lives here, in Row Level Security — the client is untrusted by definition.
--
-- Identity of content: dish autoincrement ids are NOT stable (a full
-- reclassification renumbers them), so rows key on durable natural keys:
-- the restaurant's Google place_id plus a normalized dish name (dish_key).
-- local_id carries the last-known numeric id purely as a resolution hint.
--
-- Apply in the Supabase SQL editor (whole file, idempotent-ish: run once).

-- ---------------------------------------------------------------- profiles
create table if not exists public.profiles (
  id           uuid primary key references auth.users (id) on delete cascade,
  display_name text not null default 'vegan explorer'
               check (char_length(display_name) between 1 and 40),
  created_at   timestamptz not null default now()
);

alter table public.profiles enable row level security;

drop policy if exists "profiles are readable" on public.profiles;
create policy "profiles are readable"
  on public.profiles for select using (true);

drop policy if exists "own profile is editable" on public.profiles;
create policy "own profile is editable"
  on public.profiles for update using (auth.uid() = id);

-- Auto-create a profile at signup; name from OAuth metadata or email prefix.
create or replace function public.handle_new_user()
returns trigger
language plpgsql security definer set search_path = public
as $$
begin
  insert into public.profiles (id, display_name)
  values (
    new.id,
    left(coalesce(
      nullif(new.raw_user_meta_data ->> 'full_name', ''),
      nullif(split_part(coalesce(new.email, ''), '@', 1), ''),
      'vegan explorer'
    ), 40)
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- --------------------------------------------------------------- favorites
create table if not exists public.favorites (
  user_id   uuid not null references public.profiles (id) on delete cascade,
  kind      text not null check (kind in ('dish', 'restaurant')),
  place_id  text not null check (char_length(place_id) <= 128),
  dish_key  text not null default '' check (char_length(dish_key) <= 200),
  dish_name text check (char_length(dish_name) <= 200),
  local_id  bigint,           -- last-known numeric id (resolution hint only)
  created_at timestamptz not null default now(),
  primary key (user_id, kind, place_id, dish_key)
);

alter table public.favorites enable row level security;

drop policy if exists "own favorites all ops" on public.favorites;
create policy "own favorites all ops"
  on public.favorites for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ------------------------------------------------------------------- votes
-- One live vote per user per target (mirrors the anonymous per-browser rule).
-- Readable by everyone so pages can aggregate counts client-side.
create table if not exists public.votes (
  user_id   uuid not null references public.profiles (id) on delete cascade,
  kind      text not null check (kind in ('dish', 'restaurant')),
  place_id  text not null check (char_length(place_id) <= 128),
  dish_key  text not null default '' check (char_length(dish_key) <= 200),
  dish_name text check (char_length(dish_name) <= 200),
  vote      text not null check (vote in ('up', 'down')),
  local_id  bigint,
  updated_at timestamptz not null default now(),
  primary key (user_id, kind, place_id, dish_key)
);

alter table public.votes enable row level security;

drop policy if exists "votes are readable" on public.votes;
create policy "votes are readable"
  on public.votes for select using (true);

drop policy if exists "own votes writable" on public.votes;
create policy "own votes writable"
  on public.votes for insert with check (auth.uid() = user_id);

drop policy if exists "own votes updatable" on public.votes;
create policy "own votes updatable"
  on public.votes for update using (auth.uid() = user_id);

drop policy if exists "own votes deletable" on public.votes;
create policy "own votes deletable"
  on public.votes for delete using (auth.uid() = user_id);

-- ---------------------------------------------------------------- comments
-- Per-restaurant threads; @dish mentions ride along as structured JSON
-- ([{dish_key, dish_name}]) so tips can deep-link to dishes even after ids
-- renumber. Signed-in users only (RLS insert), which kills drive-by spam.
create table if not exists public.comments (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references public.profiles (id) on delete cascade,
  place_id   text not null check (char_length(place_id) <= 128),
  body       text not null check (char_length(body) between 1 and 1000),
  mentions   jsonb not null default '[]',
  created_at timestamptz not null default now()
);

create index if not exists comments_by_place
  on public.comments (place_id, created_at desc);

alter table public.comments enable row level security;

drop policy if exists "comments are readable" on public.comments;
create policy "comments are readable"
  on public.comments for select using (true);

drop policy if exists "signed-in users comment as themselves" on public.comments;
create policy "signed-in users comment as themselves"
  on public.comments for insert with check (auth.uid() = user_id);

drop policy if exists "own comments deletable" on public.comments;
create policy "own comments deletable"
  on public.comments for delete using (auth.uid() = user_id);

-- Rate limit: RLS can't count, so a trigger holds the line (10/hour/user).
create or replace function public.enforce_comment_rate_limit()
returns trigger
language plpgsql security definer set search_path = public
as $$
begin
  if (
    select count(*) from public.comments
    where user_id = new.user_id
      and created_at > now() - interval '1 hour'
  ) >= 10 then
    raise exception 'Too many comments — try again in a bit.';
  end if;
  return new;
end;
$$;

drop trigger if exists comment_rate_limit on public.comments;
create trigger comment_rate_limit
  before insert on public.comments
  for each row execute function public.enforce_comment_rate_limit();

-- ---------------------------------------------------------- comment_reports
-- "This comment is spam/abuse" flags. Reporters see their own flags; the
-- Admin reviews via the service key (never shipped to the browser).
create table if not exists public.comment_reports (
  comment_id uuid not null references public.comments (id) on delete cascade,
  user_id    uuid not null references public.profiles (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (comment_id, user_id)
);

alter table public.comment_reports enable row level security;

drop policy if exists "report as yourself" on public.comment_reports;
create policy "report as yourself"
  on public.comment_reports for insert with check (auth.uid() = user_id);

drop policy if exists "own reports readable" on public.comment_reports;
create policy "own reports readable"
  on public.comment_reports for select using (auth.uid() = user_id);

-- ------------------------------------------------------ Data API privileges
-- This project keeps "Automatically expose new tables" OFF. Grants are a
-- separate security layer from RLS: these make only the operations used by
-- the browser reachable, while the policies above still decide which rows.
grant usage on schema public to anon, authenticated, service_role;

revoke all on table public.profiles from anon, authenticated;
grant select on table public.profiles to anon, authenticated;
grant update on table public.profiles to authenticated;

revoke all on table public.favorites from anon, authenticated;
grant select, insert, update, delete on table public.favorites to authenticated;

revoke all on table public.votes from anon, authenticated;
grant select on table public.votes to anon, authenticated;
grant insert, update, delete on table public.votes to authenticated;

revoke all on table public.comments from anon, authenticated;
grant select on table public.comments to anon, authenticated;
grant insert, delete on table public.comments to authenticated;

revoke all on table public.comment_reports from anon, authenticated;
grant select, insert on table public.comment_reports to authenticated;

-- The service role is reserved for trusted moderation/admin tooling.
grant all on table public.profiles, public.favorites, public.votes,
  public.comments, public.comment_reports to service_role;

-- These functions exist only as database triggers; browser clients never
-- call them as RPC endpoints.
revoke execute on function public.handle_new_user() from public, anon, authenticated;
revoke execute on function public.enforce_comment_rate_limit()
  from public, anon, authenticated;
