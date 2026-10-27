-- Which of a service's tools Cora may use, switched on one by one in
-- Settings > Services. A row means on; a tool without a row is off, so a
-- server adding tools never gives Cora anything new by itself.
CREATE TABLE public.service_tools (
    service TEXT NOT NULL,
    tool TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (service, tool),
    CONSTRAINT service_tools_service_check CHECK (btrim(service) <> ''),
    CONSTRAINT service_tools_tool_check CHECK (btrim(tool) <> '')
);
