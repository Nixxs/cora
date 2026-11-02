-- A desire can be a nearer version of another: when the person offers something else instead
-- (not the reef, but Rottnest), the new desire links to the one it was adapted from, which stays.
ALTER TABLE public.desires
    ADD COLUMN adapted_from UUID,
    ADD CONSTRAINT desires_adapted_from_fkey
        FOREIGN KEY (adapted_from) REFERENCES public.desires (id) ON DELETE RESTRICT,
    ADD CONSTRAINT desires_adapted_from_check CHECK (adapted_from IS NULL OR adapted_from <> id);
