BEGIN;

-- ============================================================
-- Migration tracking
-- ============================================================

CREATE TABLE schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================================================
-- 1. Campaigns
-- One row per campaign.
-- ============================================================

CREATE TABLE campaigns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    name TEXT NOT NULL
        CHECK (BTRIM(name) <> ''),

    subject TEXT NOT NULL
        CHECK (BTRIM(subject) <> ''),

    html_body TEXT NOT NULL
        CHECK (BTRIM(html_body) <> ''),

    from_email TEXT NOT NULL
        CHECK (BTRIM(from_email) <> ''),

    tracking_enabled BOOLEAN NOT NULL DEFAULT TRUE,

    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'sending', 'sent', 'failed')),

    idempotency_key TEXT NOT NULL UNIQUE
        CHECK (BTRIM(idempotency_key) <> ''),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ============================================================
-- 2. Campaign links
-- Only pre-approved HTTPS destinations may be stored.
-- link_no identifies a link within its campaign.
-- ============================================================

CREATE TABLE campaign_links (
    campaign_id UUID NOT NULL
        REFERENCES campaigns(id) ON DELETE CASCADE,

    link_no INTEGER NOT NULL
        CHECK (link_no > 0),

    destination_url TEXT NOT NULL
        CHECK (
            destination_url = BTRIM(destination_url)
            AND destination_url ~* '^https://'
        ),

    PRIMARY KEY (campaign_id, link_no)
);


-- ============================================================
-- 3. Recipients
-- Only approved, opted-in recipients may receive campaigns.
-- The API must explicitly set opted_in when registering an
-- approved test recipient; the database defaults to false.
-- ============================================================

CREATE TABLE recipients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    email TEXT NOT NULL UNIQUE
        CHECK (BTRIM(email) <> ''),

    opted_in BOOLEAN NOT NULL DEFAULT FALSE
);


-- ============================================================
-- 4. Messages
-- One row per campaign-recipient pair.
-- id is our internal message ID.
-- token is an opaque, randomly generated tracking token.
-- ============================================================

CREATE TABLE messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    campaign_id UUID NOT NULL
        REFERENCES campaigns(id),

    recipient_id UUID NOT NULL
        REFERENCES recipients(id),

    token TEXT NOT NULL UNIQUE
        CHECK (BTRIM(token) <> ''),

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_messages_campaign_recipient
        UNIQUE (campaign_id, recipient_id)
);


-- ============================================================
-- 5. Events
-- Append-only history of submission, delivery and tracking.
-- ============================================================

CREATE TABLE events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    message_id UUID NOT NULL
        REFERENCES messages(id),

    event_type TEXT NOT NULL
        CHECK (
            event_type IN (
                'submitted',
                'submit_error',
                'mta_accepted',
                'remote_accepted',
                'deferred',
                'failed',
                'open',
                'click'
            )
        ),

    occurred_at TIMESTAMPTZ NOT NULL,

    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    source TEXT NOT NULL
        CHECK (BTRIM(source) <> ''),

    idempotency_key TEXT NOT NULL UNIQUE
        CHECK (BTRIM(idempotency_key) <> ''),

    smtp_code INTEGER
        CHECK (smtp_code IS NULL OR smtp_code BETWEEN 100 AND 599),

    smtp_response TEXT,

    link_no INTEGER
        CHECK (link_no IS NULL OR link_no > 0),

    user_agent TEXT
);


-- ============================================================
-- Indexes
-- ============================================================

CREATE INDEX idx_campaigns_status_created
    ON campaigns (status, created_at DESC);

CREATE INDEX idx_messages_recipient
    ON messages (recipient_id);

CREATE INDEX idx_events_message_occurred
    ON events (message_id, occurred_at DESC, ingested_at DESC);

CREATE INDEX idx_events_type_occurred
    ON events (event_type, occurred_at DESC);


-- ============================================================
-- Enforce append-only event history
-- UPDATE and DELETE are rejected.
-- TRUNCATE is rejected separately.
-- ============================================================

CREATE FUNCTION prevent_event_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'The events table is append-only; operation % is not allowed',
        TG_OP;
END;
$$;

CREATE TRIGGER events_no_update_delete
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW
    EXECUTE FUNCTION prevent_event_mutation();

CREATE TRIGGER events_no_truncate
    BEFORE TRUNCATE ON events
    FOR EACH STATEMENT
    EXECUTE FUNCTION prevent_event_mutation();


-- ============================================================
-- View 1: v_events
-- Provides event data without exposing recipient email addresses.
-- ============================================================

CREATE VIEW v_events AS
SELECT
    e.id AS event_id,
    c.id AS campaign_id,
    e.message_id,
    e.event_type,
    e.occurred_at,
    e.ingested_at,
    e.source,
    e.smtp_code,
    e.smtp_response,
    e.link_no,
    e.user_agent,

    CASE
        WHEN POSITION('@' IN r.email) > 0
        THEN LOWER(SPLIT_PART(r.email, '@', 2))
        ELSE NULL
    END AS recipient_domain

FROM events AS e
JOIN messages AS m
    ON m.id = e.message_id
JOIN campaigns AS c
    ON c.id = m.campaign_id
JOIN recipients AS r
    ON r.id = m.recipient_id;


-- ============================================================
-- View 2: v_failures
-- Includes temporary deferrals and reported failures.
-- ============================================================

CREATE VIEW v_failures AS
SELECT
    event_id,
    campaign_id,
    message_id,
    event_type,
    occurred_at,
    ingested_at,
    source,
    smtp_code,
    smtp_response,
    recipient_domain

FROM v_events

WHERE event_type IN (
    'deferred',
    'failed',
    'submit_error'
);


-- ============================================================
-- View 3: v_message_status
-- Derives message status from its event history.
-- Open and click observations do not change delivery status.
-- ============================================================

CREATE VIEW v_message_status AS
SELECT
    m.id AS message_id,
    m.campaign_id,

    c.status AS campaign_status,

    CASE
        WHEN POSITION('@' IN r.email) > 0
        THEN LOWER(SPLIT_PART(r.email, '@', 2))
        ELSE NULL
    END AS recipient_domain,

    COALESCE(last_event.event_type, 'pending')
        AS message_status,

    last_event.occurred_at AS status_occurred_at,
    last_event.source AS status_source,
    last_event.smtp_code,
    last_event.smtp_response,

    m.created_at AS message_created_at

FROM messages AS m

JOIN campaigns AS c
    ON c.id = m.campaign_id

JOIN recipients AS r
    ON r.id = m.recipient_id

LEFT JOIN LATERAL (
    SELECT
        e.event_type,
        e.occurred_at,
        e.ingested_at,
        e.id,
        e.source,
        e.smtp_code,
        e.smtp_response

    FROM events AS e

    WHERE e.message_id = m.id

      AND e.event_type IN (
          'submitted',
          'submit_error',
          'mta_accepted',
          'remote_accepted',
          'deferred',
          'failed'
      )

    ORDER BY
        e.occurred_at DESC,
        e.ingested_at DESC,
        e.id DESC

    LIMIT 1
) AS last_event ON TRUE;


-- ============================================================
-- Record this migration as applied.
-- ============================================================

INSERT INTO schema_migrations (version)
VALUES ('001_init');

COMMIT;