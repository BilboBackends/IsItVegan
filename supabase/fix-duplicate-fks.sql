-- One-time repair: schema.sql (SQL editor) and the migrations were BOTH
-- applied, so the user-data tables carry duplicate foreign keys to
-- profiles/comments. PostgREST then refuses relationship embeds with
-- "more than one relationship was found for 'comments' and 'profiles'".
--
-- For each (table -> referenced table) pair with more than one FK, keeps
-- the first constraint alphabetically and drops the rest. Idempotent —
-- running it again finds nothing to drop.

do $$
declare
  dup record;
  extra record;
  kept text;
begin
  for dup in
    select conrelid, confrelid
    from pg_constraint
    where contype = 'f'
      and conrelid in (
        'public.favorites'::regclass, 'public.votes'::regclass,
        'public.comments'::regclass, 'public.comment_reports'::regclass
      )
    group by conrelid, confrelid
    having count(*) > 1
  loop
    kept := null;
    for extra in
      select conname
      from pg_constraint
      where contype = 'f'
        and conrelid = dup.conrelid
        and confrelid = dup.confrelid
      order by conname
    loop
      if kept is null then
        kept := extra.conname;
        raise notice 'keeping FK % on %', extra.conname, dup.conrelid::regclass;
      else
        execute format(
          'alter table %s drop constraint %I',
          dup.conrelid::regclass, extra.conname
        );
        raise notice 'dropped duplicate FK % on %',
          extra.conname, dup.conrelid::regclass;
      end if;
    end loop;
  end loop;
end $$;

-- Verify: each (table, references) pair should now appear exactly once.
select conrelid::regclass  as table_name,
       confrelid::regclass as references_table,
       conname
from pg_constraint
where contype = 'f'
  and conrelid in (
    'public.favorites'::regclass, 'public.votes'::regclass,
    'public.comments'::regclass, 'public.comment_reports'::regclass
  )
order by 1, 2;
