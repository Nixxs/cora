-- Cora's desires: future states or experiences she'd like to pursue, growing from her
-- interests and experience. Strength is a snapshot at strength_at; fading, promotion
-- from candidate to held and the caps live in application code (desire_rules).
-- A desire that ends (fulfilled or let go) keeps its row and history.
CREATE TABLE public.desires (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    description TEXT NOT NULL,
    motivation TEXT NOT NULL,
    strength DOUBLE PRECISION NOT NULL,
    strength_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_reinforced_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    CONSTRAINT desires_description_check CHECK (btrim(description) <> '' AND char_length(description) <= 300),
    CONSTRAINT desires_motivation_check CHECK (btrim(motivation) <> '' AND char_length(motivation) <= 1000),
    CONSTRAINT desires_strength_check CHECK (strength >= 0 AND strength <= 1),
    CONSTRAINT desires_status_check CHECK (status IN ('candidate', 'held', 'pursuing', 'fulfilled', 'let_go')),
    CONSTRAINT desires_ended_check CHECK (
        (status IN ('fulfilled', 'let_go')) = (ended_at IS NOT NULL)
        AND (ended_at IS NULL OR ended_at >= created_at)
    )
);

CREATE INDEX desires_status_idx ON public.desires (status);

-- Append-only history of each desire: how it formed, grew, changed status or faded,
-- with progress notes while she pursues it. note says why, or what happened.
CREATE TABLE public.desire_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    desire_id UUID NOT NULL,
    kind TEXT NOT NULL,
    strength_before DOUBLE PRECISION,
    strength_after DOUBLE PRECISION NOT NULL,
    note TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT desire_events_desire_id_fkey
        FOREIGN KEY (desire_id) REFERENCES public.desires (id) ON DELETE RESTRICT,
    CONSTRAINT desire_events_kind_check CHECK (kind IN (
        'form', 'reinforce', 'hold', 'pursue', 'pause', 'progress', 'fulfil', 'let_go', 'fade'
    )),
    CONSTRAINT desire_events_strength_check CHECK (strength_after >= 0 AND strength_after <= 1),
    CONSTRAINT desire_events_before_check CHECK (
        (kind = 'form') = (strength_before IS NULL)
        AND (strength_before IS NULL OR (strength_before >= 0 AND strength_before <= 1))
    ),
    CONSTRAINT desire_events_note_check CHECK (btrim(note) <> '' AND char_length(note) <= 1000)
);

CREATE INDEX desire_events_desire_id_created_at_idx ON public.desire_events (desire_id, created_at);

-- What a desire grew from: exactly one of an interest, a finding or a conversation per row.
CREATE TABLE public.desire_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    desire_id UUID NOT NULL,
    interest_id UUID,
    exploration_id UUID,
    conversation_id UUID,
    CONSTRAINT desire_sources_desire_id_fkey
        FOREIGN KEY (desire_id) REFERENCES public.desires (id) ON DELETE RESTRICT,
    CONSTRAINT desire_sources_interest_id_fkey
        FOREIGN KEY (interest_id) REFERENCES public.interests (id) ON DELETE RESTRICT,
    CONSTRAINT desire_sources_exploration_id_fkey
        FOREIGN KEY (exploration_id) REFERENCES public.interest_explorations (id) ON DELETE RESTRICT,
    CONSTRAINT desire_sources_conversation_id_fkey
        FOREIGN KEY (conversation_id) REFERENCES public.conversation_summaries (conversation_id) ON DELETE RESTRICT,
    CONSTRAINT desire_sources_source_check CHECK (num_nonnulls(interest_id, exploration_id, conversation_id) = 1)
);

CREATE INDEX desire_sources_desire_id_idx ON public.desire_sources (desire_id);
