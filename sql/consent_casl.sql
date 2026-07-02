-- ============================================================================
-- CASL consent & unsubscribe infrastructure (2026-07-02)
-- Apply locally via psql; apply to Railway manually (never via deploy_sp.ps1).
-- ============================================================================

-- Global suppression list — one row per opted-out address. Checked by
-- app/core/consent.py before any COMMERCIAL outbound email is sent.
CREATE TABLE IF NOT EXISTS email_suppression (
    email       TEXT PRIMARY KEY,           -- stored lowercase
    reason      TEXT        NOT NULL DEFAULT 'unsubscribed',
    source      TEXT        NOT NULL DEFAULT 'unsubscribe_link',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Consent capture fields (data model ready; form capture is a follow-up).
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS email_consent      BOOLEAN;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS consent_source     TEXT;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS consent_at         TIMESTAMPTZ;
ALTER TABLE leads    ADD COLUMN IF NOT EXISTS email_consent      BOOLEAN;
ALTER TABLE leads    ADD COLUMN IF NOT EXISTS consent_source     TEXT;
ALTER TABLE leads    ADD COLUMN IF NOT EXISTS consent_at         TIMESTAMPTZ;

-- Verify
SELECT 'email_suppression' AS object, count(*) AS rows FROM email_suppression;
