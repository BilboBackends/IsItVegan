-- Make newly granted tables visible immediately to PostgREST.
notify pgrst, 'reload schema';
