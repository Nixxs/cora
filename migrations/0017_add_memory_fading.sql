-- A time-bound memory (for example an appointment date) fades once its moment
-- has passed. The row is kept; recall just stops putting it forward.
-- fades_at stays NULL for lasting memories; fade_checked_at records the check.
ALTER TABLE public.memories
    ADD COLUMN fades_at TIMESTAMPTZ,
    ADD COLUMN fade_checked_at TIMESTAMPTZ,
    ADD CONSTRAINT memories_fade_check CHECK (
        fades_at IS NULL OR fade_checked_at IS NOT NULL
    );
