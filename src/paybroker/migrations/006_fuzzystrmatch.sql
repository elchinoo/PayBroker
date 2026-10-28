-- fuzzystrmatch (levenshtein, dmetaphone, soundex) powers the NAME_MISMATCH risk signal (namematch.py). It ships with Postgres and
-- is available on AgentPG for the instance role. If a database cannot create it, the migration still succeeds and the signal is
-- simply never raised (namematch checks pg_extension before using it).
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS fuzzystrmatch;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'fuzzystrmatch not available (%): the NAME_MISMATCH signal is disabled', SQLERRM;
END
$$;
