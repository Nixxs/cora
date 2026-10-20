-- A newer memory may correct an older one (for example a new home city).
-- The newer row points at the memory it replaces; both rows are kept, so the
-- history stays available while recall can prefer the newer statement.
ALTER TABLE public.memories
    ADD COLUMN corrects UUID,
    ADD COLUMN correction_checked_at TIMESTAMPTZ,
    ADD CONSTRAINT memories_corrects_fkey FOREIGN KEY (corrects)
        REFERENCES public.memories (id) ON DELETE RESTRICT,
    ADD CONSTRAINT memories_correction_check CHECK (
        corrects IS NULL OR (corrects <> id AND correction_checked_at IS NOT NULL)
    );
