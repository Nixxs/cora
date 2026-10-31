-- Webhooks, set up in Settings > Services: whether Cora listens, the public HTTPS address that
-- reaches this PC, and the local port she listens on (127.0.0.1 only; the person routes public
-- traffic to it). One row at most. The secrets in her webhook URLs live in the system credential
-- store, never here.
CREATE TABLE public.webhook_settings (
    id SMALLINT PRIMARY KEY DEFAULT 1,
    enabled BOOLEAN NOT NULL DEFAULT false,
    public_url TEXT,
    port INTEGER NOT NULL DEFAULT 8000,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT webhook_settings_single_row CHECK (id = 1),
    CONSTRAINT webhook_settings_https CHECK (public_url IS NULL OR public_url LIKE 'https://%'),
    CONSTRAINT webhook_settings_port CHECK (port BETWEEN 1024 AND 65535)
);
