-- Which model each area of Cora uses. An area without a row keeps the default
-- from application code, so a fresh install needs no seed. The list of allowed
-- models lives in code, so it can change without a migration.
CREATE TABLE public.model_settings (
    area TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT model_settings_area_check CHECK (area IN ('delegation', 'memory', 'subconscious', 'utility')),
    CONSTRAINT model_settings_model_check CHECK (btrim(model) <> '')
);
