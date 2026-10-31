-- Cora's impression of each person she talks with as a client, in her own words: how they
-- come across, roughly what they talk about and what she thinks of them, never a record of
-- what they said. opinion is a separate one-line view fit to share with anyone she talks
-- with; impression is for the person she works with and the client's own chat. One generic
-- row per person, revised from each finished conversation with them.
CREATE TABLE public.impressions (
    person_id UUID PRIMARY KEY REFERENCES public.people (id) ON DELETE CASCADE,
    impression TEXT NOT NULL,
    opinion TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT impressions_impression_check CHECK (btrim(impression) <> '' AND char_length(impression) <= 1200),
    CONSTRAINT impressions_opinion_check CHECK (btrim(opinion) <> '' AND char_length(opinion) <= 200)
);

-- Each finished client conversation that has shaped an impression, so each counts once.
CREATE TABLE public.impression_updates (
    conversation_id UUID PRIMARY KEY REFERENCES public.conversations (id) ON DELETE CASCADE,
    person_id UUID NOT NULL REFERENCES public.people (id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
