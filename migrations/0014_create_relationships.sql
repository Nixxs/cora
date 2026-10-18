-- How Cora feels about each person, derived from appraisals of their everyday
-- conversations. Each value runs from 0 to 1; the baselines, step sizes and slow
-- drift back to baseline live in application code. Familiarity only grows.
CREATE TABLE public.relationships (
    person_id UUID PRIMARY KEY,
    trust DOUBLE PRECISION NOT NULL,
    warmth DOUBLE PRECISION NOT NULL,
    familiarity DOUBLE PRECISION NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT relationships_person_id_fkey
        FOREIGN KEY (person_id) REFERENCES public.people (id) ON DELETE RESTRICT,
    CONSTRAINT relationships_values_check CHECK (
        trust >= 0 AND trust <= 1 AND warmth >= 0 AND warmth <= 1
        AND familiarity >= 0 AND familiarity <= 1
    )
);

-- Append-only history. Values are the state after the event, so every event is
-- also a snapshot. An appraisal is applied at most once; a reset needs a reason
-- and never deletes history or appraisals.
CREATE TABLE public.relationship_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id UUID NOT NULL,
    kind TEXT NOT NULL,
    conversation_id UUID,
    trust DOUBLE PRECISION NOT NULL,
    warmth DOUBLE PRECISION NOT NULL,
    familiarity DOUBLE PRECISION NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT relationship_events_person_id_fkey
        FOREIGN KEY (person_id) REFERENCES public.relationships (person_id) ON DELETE RESTRICT,
    CONSTRAINT relationship_events_conversation_id_fkey
        FOREIGN KEY (conversation_id) REFERENCES public.interaction_appraisals (conversation_id) ON DELETE RESTRICT,
    CONSTRAINT relationship_events_conversation_id_key UNIQUE (conversation_id),
    CONSTRAINT relationship_events_kind_check CHECK (
        kind IN ('appraisal', 'reset')
        AND (kind = 'appraisal') = (conversation_id IS NOT NULL)
        AND (kind = 'reset') = (reason IS NOT NULL)
    ),
    CONSTRAINT relationship_events_values_check CHECK (
        trust >= 0 AND trust <= 1 AND warmth >= 0 AND warmth <= 1
        AND familiarity >= 0 AND familiarity <= 1
    ),
    CONSTRAINT relationship_events_reason_check CHECK (
        reason IS NULL OR (btrim(reason) <> '' AND char_length(reason) <= 1000)
    )
);

CREATE INDEX relationship_events_person_id_created_at_idx
    ON public.relationship_events (person_id, created_at);
