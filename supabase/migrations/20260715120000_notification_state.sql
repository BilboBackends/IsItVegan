begin;

-- Cross-device notification read state: one row per user recording when
-- they last opened the notifications panel. Replies and @mentions newer
-- than seen_at light the header badge on any signed-in device. This is a
-- separate table rather than a profiles column so the timestamp is not
-- world-readable through the "profiles are readable" policy.
create table if not exists public.notification_state (
  user_id uuid primary key references public.profiles (id) on delete cascade,
  seen_at timestamptz not null default now()
);

alter table public.notification_state enable row level security;

drop policy if exists "own notification state" on public.notification_state;
create policy "own notification state"
  on public.notification_state for all
  using (auth.uid() = user_id) with check (auth.uid() = user_id);

revoke all on table public.notification_state from anon, authenticated;
grant select, insert, update on table public.notification_state to authenticated;
grant all on table public.notification_state to service_role;

notify pgrst, 'reload schema';

commit;
