-- At most one row: when Cora last reflected on what she wants, whether or not anything changed.
CREATE TABLE public.desire_worker_state (
    id SMALLINT PRIMARY KEY DEFAULT 1,
    last_reflected_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT desire_worker_state_singleton_check CHECK (id = 1)
);
