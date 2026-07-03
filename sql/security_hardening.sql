-- ============================================================================
-- Security hardening — consolidated DDL (run this ONE file on Railway).
--
-- Bundles all auth-related schema changes in dependency order. Idempotent and
-- safe to run repeatedly. Supersedes running sql/auth_sessions.sql +
-- sql/rbac_roles.sql separately (those remain for reference).
--
--   #1b  DB-backed sessions   — auth_sessions table (replaces the in-memory
--                               session dict; survives restarts / multi-instance;
--                               stores only the SHA-256 token hash).
--   #2   RBAC role tiers      — access_role on the login + role on the session.
--                               Tiers: admin | member (default) | viewer.
--
-- NOTE: the runtime switches are ENVIRONMENT variables, not schema — set them on
-- Railway after deploying the backend:
--   ADMIN_API_TOKEN=<strong secret>   locks the command endpoints immediately
--   API_AUTH_ENABLED=1                enforces user sessions on the data endpoints
--                                     (flip only after the frontend is deployed)
-- ============================================================================


-- ── #1b · DB-backed session store ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash     TEXT        PRIMARY KEY,     -- sha256(token); plaintext never stored
    account_id     TEXT,
    credential_id  TEXT,
    identifier     TEXT,
    lead_id        TEXT,
    contact_id     TEXT,
    first_name     TEXT,
    last_name      TEXT,
    source_table   TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions (expires_at);


-- ── #2 · RBAC role tiers ─────────────────────────────────────────────────────
-- access_role lives on the login identity (distinct from the CRM job-title
-- contacts.role / employees.role). Resolved at /auth/signin, carried in the
-- session, enforced by app/core/auth_dep.py.
ALTER TABLE auth_credentials ADD COLUMN IF NOT EXISTS access_role TEXT NOT NULL DEFAULT 'member';
ALTER TABLE auth_sessions    ADD COLUMN IF NOT EXISTS role        TEXT NOT NULL DEFAULT 'member';

-- Demo posture (public-read / authorized-write): NEW self-signups are read-only.
-- Authorized writers are then promoted by an admin (see below). Idempotent.
ALTER TABLE auth_credentials ALTER COLUMN access_role SET DEFAULT 'viewer';


-- ── Session governance · idle timeout ───────────────────────────────────────
-- get_session() slides this forward on activity; sessions idle for more than
-- AUTH_IDLE_MINUTES (env, default 15) are invalidated on next use.
ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now();


-- ── Assign roles (edit identifiers, then run these lines) ────────────────────
--   UPDATE auth_credentials SET access_role = 'admin'  WHERE identifier = 'you@company.com';
--   UPDATE auth_credentials SET access_role = 'member' WHERE identifier = 'writer@company.com';
--
-- ONE-TIME (optional, for the demo): make every existing non-admin login
-- read-only, so only Admin + people you explicitly promote can write. Run ONCE —
-- re-running would undo any 'member' grants you have made since.
--   UPDATE auth_credentials SET access_role = 'viewer' WHERE access_role <> 'admin';


-- ── Verify ───────────────────────────────────────────────────────────────────
SELECT
    to_regclass('auth_sessions')                                              AS auth_sessions_table,
    (SELECT count(*) FROM information_schema.columns
      WHERE table_name = 'auth_sessions'   AND column_name = 'role')          AS has_session_role,
    (SELECT count(*) FROM information_schema.columns
      WHERE table_name = 'auth_credentials' AND column_name = 'access_role')  AS has_access_role,
    (SELECT count(*) FROM information_schema.columns
      WHERE table_name = 'auth_sessions'   AND column_name = 'last_seen_at')  AS has_last_seen;
