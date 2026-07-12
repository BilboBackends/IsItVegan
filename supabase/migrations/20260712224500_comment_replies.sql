begin;

alter table public.comments
  add column if not exists parent_comment_id uuid;

alter table public.comments
  drop constraint if exists comments_parent_is_not_self;
alter table public.comments
  add constraint comments_parent_is_not_self check (
    parent_comment_id is null or parent_comment_id <> id
  );

create index if not exists comments_by_parent
  on public.comments (parent_comment_id, created_at)
  where parent_comment_id is not null;

-- There is deliberately no delete-cascading foreign key: comments are hard
-- deleted today, and replies should remain visibly replies when the original
-- note disappears. Since browser clients cannot update comments, validating
-- the parent UUID and restaurant on insert preserves the relationship safely.
create or replace function public.validate_comment_reply()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  parent_place_id text;
begin
  if new.parent_comment_id is null then
    return new;
  end if;

  select c.place_id into parent_place_id
  from public.comments c
  where c.id = new.parent_comment_id;

  if parent_place_id is null then
    raise exception using errcode = '23503', message = 'The original note is no longer available.';
  end if;
  if parent_place_id <> new.place_id then
    raise exception using errcode = '23514', message = 'Replies must stay in the same restaurant.';
  end if;

  return new;
end;
$$;

drop trigger if exists validate_comment_reply on public.comments;
create trigger validate_comment_reply
  before insert or update of parent_comment_id, place_id on public.comments
  for each row execute function public.validate_comment_reply();

revoke execute on function public.validate_comment_reply()
  from public, anon, authenticated;

notify pgrst, 'reload schema';

commit;
