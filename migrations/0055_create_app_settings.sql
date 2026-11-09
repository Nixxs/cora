-- General app settings set in Settings > General, one row per setting. A setting without a row
-- keeps its default from application code, so a fresh install needs no seed; which values are
-- allowed lives in code too, so it can change without a migration.
CREATE TABLE public.app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT app_settings_key_check CHECK (key IN ('voice')),
    CONSTRAINT app_settings_value_check CHECK (btrim(value) <> '' AND char_length(value) <= 200)
);
