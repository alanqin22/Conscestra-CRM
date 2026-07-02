-- ============================================================================
-- Company profile — single-row org identity (CASL sender identification).
-- Managed from executives-mgmt.html; read by app/core/consent.py for the
-- commercial-email footer. Apply locally via psql; Railway manually.
-- ============================================================================

CREATE TABLE IF NOT EXISTS company_profile (
    profile_id       INT PRIMARY KEY DEFAULT 1 CHECK (profile_id = 1),  -- single row
    company_name     TEXT NOT NULL DEFAULT 'Conscestra CRM',
    mailing_address  TEXT NOT NULL DEFAULT '',
    contact_email    TEXT NOT NULL DEFAULT 'info@agentorc.ca',
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by       TEXT
);

INSERT INTO company_profile (profile_id, company_name, mailing_address, contact_email)
VALUES (1, 'Conscestra CRM', 'Conscestra CRM, Toronto, ON, Canada', 'info@agentorc.ca')
ON CONFLICT (profile_id) DO NOTHING;

SELECT * FROM company_profile;
