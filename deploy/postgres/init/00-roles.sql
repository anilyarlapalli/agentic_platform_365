-- Roles and extensions. Runs once, on an empty data directory, as superuser.
--
-- ── Why two roles ─────────────────────────────────────────────────────────
--
-- Row-level security is the isolation control for this platform, and it has two
-- documented bypasses:
--
--   1. A SUPERUSER bypasses RLS unconditionally. Always.
--   2. The table OWNER bypasses RLS unless the table is explicitly marked
--      FORCE ROW LEVEL SECURITY.
--
-- `platform_owner` is the bootstrap superuser: it owns the schema and runs
-- migrations. If the application connected as that role, every policy written
-- in every migration would be decorative — the tests would pass against an
-- unenforced control, which is worse than no control because it reads as one.
--
-- So the runtime connects as `platform_app`: NOSUPERUSER, NOBYPASSRLS, owns
-- nothing. Migrations additionally mark every tenant table FORCE ROW LEVEL
-- SECURITY, so even a future mistake that makes `platform_app` an owner does
-- not silently reopen the boundary.
--
-- `tests/properties/test_tenant_isolation.py` asserts both roles behave as
-- described, because "we use RLS" is a claim and "the superuser bypass is not
-- reachable from the app connection" is the property.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid(), digest()

-- The runtime role. NOBYPASSRLS is the default, but stated so that a reader
-- does not have to know that, and so a future ALTER that grants it is a visible
-- diff against an explicit baseline.
CREATE ROLE platform_app
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOBYPASSRLS
    PASSWORD 'platform_dev_only';

-- A read-only role for the eval/reporting surface. Same RLS, no writes: a gate
-- that can mutate the thing it grades is not a gate.
CREATE ROLE platform_readonly
    LOGIN
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOBYPASSRLS
    PASSWORD 'platform_dev_only';

GRANT CONNECT ON DATABASE platform TO platform_app, platform_readonly;
GRANT USAGE ON SCHEMA public TO platform_app, platform_readonly;

-- Future tables created by the owner are usable by the app without every
-- migration having to remember a GRANT. Forgetting one would surface as a
-- permission error rather than a security hole — but it would surface at
-- runtime, which is late.
ALTER DEFAULT PRIVILEGES FOR ROLE platform_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO platform_app;
ALTER DEFAULT PRIVILEGES FOR ROLE platform_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO platform_app;
ALTER DEFAULT PRIVILEGES FOR ROLE platform_owner IN SCHEMA public
    GRANT SELECT ON TABLES TO platform_readonly;

-- The tenant discriminator. Policies read `current_setting('app.tenant_id')`,
-- which is set per transaction by `platform.db.engine.tenant_session`. The
-- `true` second argument to current_setting makes a missing setting return NULL
-- rather than raise — and NULL never equals a tenant id, so an unset context
-- returns zero rows instead of every row. Fail-closed by construction.
