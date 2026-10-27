-- Cora's own Signal account, set up in Settings > Services. One row at most.
-- account is the number linked from Cora's phone, never the person's; allowed_number
-- is the person's own number, the only sender she answers for now. Her Signal keys
-- live in signal-cli's Docker volume, never here.
CREATE TABLE public.signal_settings (
    id SMALLINT PRIMARY KEY DEFAULT 1,
    enabled BOOLEAN NOT NULL DEFAULT false,
    account TEXT,
    allowed_number TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT signal_settings_single_row CHECK (id = 1),
    CONSTRAINT signal_settings_numbers_differ CHECK (account IS NULL OR account <> allowed_number)
);
