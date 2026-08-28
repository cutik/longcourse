-- Read-only role for Grafana.
--
-- Run once, as the longcourse owner:
--   docker exec -it app_db21ed7f_postgres_latest \
--     psql -U longcourse -d longcourse -f /path/grafana_role.sql
-- or paste it into psql. Set a real password first.
--
-- Grafana points at TeslaMate's existing instance, so the datasource it uses
-- must not be able to write to the health database — a dashboard panel is a
-- rawSql field that anyone with edit rights can change into a DELETE.

\set ro_password 'CHANGE-ME'

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'longcourse_ro') THEN
        CREATE ROLE longcourse_ro LOGIN;
    END IF;
END $$;

ALTER ROLE longcourse_ro WITH PASSWORD :'ro_password';

GRANT CONNECT ON DATABASE longcourse TO longcourse_ro;
GRANT USAGE ON SCHEMA public TO longcourse_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO longcourse_ro;

-- New tables and views are created on every boot by apply_schema(), so the
-- grant has to apply to future objects too or a dashboard breaks the next time
-- the schema grows. Default privileges are scoped to the creating role, so this
-- must name `longcourse` (the app role that owns the tables) — a bare
-- ALTER DEFAULT PRIVILEGES would only cover objects created by whoever runs
-- this script.
ALTER DEFAULT PRIVILEGES FOR ROLE longcourse IN SCHEMA public
    GRANT SELECT ON TABLES TO longcourse_ro;

-- Belt and braces: revoke anything that would let a panel mutate data.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public
    FROM longcourse_ro;
REVOKE CREATE ON SCHEMA public FROM longcourse_ro;
