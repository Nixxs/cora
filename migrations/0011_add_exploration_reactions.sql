-- Cora's own brief take on a finding: what she thought of it and why. Optional (older
-- findings have none) and kept as evidence for her later reflection on who she is.
ALTER TABLE public.interest_explorations
    ADD COLUMN reaction TEXT,
    ADD CONSTRAINT interest_explorations_reaction_check CHECK (
        reaction IS NULL OR (status = 'found' AND btrim(reaction) <> '' AND char_length(reaction) <= 500)
    );
