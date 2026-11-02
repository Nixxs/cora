-- Interests can now grow from Cora's own exploring, not only from the person's conversations.
-- origin: 'conversation' (sparked by the person, as every earlier interest was) or 'own'
-- (sparked by something she came across while exploring).
ALTER TABLE public.interests
    ADD COLUMN origin TEXT NOT NULL DEFAULT 'conversation',
    ADD CONSTRAINT interests_origin_check CHECK (origin IN ('conversation', 'own'));

-- tangents: up to two other things she noticed along the way and would like to look into,
-- each {"title", "why"}. pull: how much the finding made her want to keep going ('none',
-- 'some', 'strong'); a little strength for the interest, within a ceiling. sparks_used_at:
-- when the interest cycle last weighed its tangents, so each is offered once.
ALTER TABLE public.interest_explorations
    ADD COLUMN tangents JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN pull TEXT,
    ADD COLUMN sparks_used_at TIMESTAMPTZ,
    ADD CONSTRAINT interest_explorations_tangents_check CHECK (
        jsonb_typeof(tangents) = 'array' AND jsonb_array_length(tangents) <= 2
        AND (status = 'found' OR jsonb_array_length(tangents) = 0)
    ),
    ADD CONSTRAINT interest_explorations_pull_check CHECK (
        pull IS NULL OR (status = 'found' AND pull IN ('none', 'some', 'strong'))
    );

CREATE INDEX interest_explorations_unused_sparks_idx
    ON public.interest_explorations (finished_at)
    WHERE sparks_used_at IS NULL AND jsonb_array_length(tangents) > 0;
