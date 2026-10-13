ALTER TABLE public.memories
    ADD COLUMN duplicate_of UUID,
    ADD COLUMN dedup_checked_at TIMESTAMPTZ,
    ADD CONSTRAINT memories_duplicate_of_fkey FOREIGN KEY (duplicate_of)
        REFERENCES public.memories (id) ON DELETE RESTRICT,
    ADD CONSTRAINT memories_duplicate_check CHECK (
        duplicate_of IS NULL OR (duplicate_of <> id AND dedup_checked_at IS NOT NULL)
    );