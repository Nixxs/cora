-- Which outside services (MCP servers) are switched on in Settings > Services.
-- A service without a row is off. The list of services lives in code, and
-- their tokens live in the operating system's credential store, never here.
CREATE TABLE public.service_settings (
    service TEXT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT false,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT service_settings_service_check CHECK (btrim(service) <> '')
);
