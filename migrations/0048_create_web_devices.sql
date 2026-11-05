-- The web interface (Settings > Services > Web interface): whether it is on, and the devices paired
-- with it. The listener runs when webhooks or the web interface are on.
ALTER TABLE public.webhook_settings
    ADD COLUMN web_enabled BOOLEAN NOT NULL DEFAULT false;

-- A paired device holds a long random token in a cookie; only its SHA-256 is kept here, so this
-- table alone can't sign anyone in. Removing a row unpairs the device.
CREATE TABLE public.web_devices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    paired_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT web_devices_token_hash_key UNIQUE (token_hash),
    CONSTRAINT web_devices_token_hash_check CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT web_devices_name_check CHECK (btrim(name) <> '' AND char_length(name) <= 60)
);
