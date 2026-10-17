-- Cora's sense of who she is: traits she has formed from her own experience.
-- Her foundation lives in code and is never stored here. A trait is "held" (and
-- reaches conversations) while active with confidence >= 0.5; the rules that keep
-- traits small, slow and evidenced live in application code.
CREATE TABLE public.identity_traits (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind TEXT NOT NULL,
    statement TEXT NOT NULL,
    polarity TEXT,
    subject TEXT,
    confidence DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    retired_at TIMESTAMPTZ,
    CONSTRAINT identity_traits_kind_check CHECK (kind IN ('value', 'preference', 'perspective', 'tendency')),
    CONSTRAINT identity_traits_statement_check CHECK (btrim(statement) <> '' AND char_length(statement) <= 300),
    -- Only preferences have a polarity and a subject (what she likes or dislikes).
    CONSTRAINT identity_traits_preference_check CHECK (
        (kind = 'preference') = (polarity IS NOT NULL)
        AND (kind = 'preference') = (subject IS NOT NULL)
        AND (polarity IS NULL OR polarity IN ('like', 'dislike'))
        AND (subject IS NULL OR (btrim(subject) <> '' AND char_length(subject) <= 200))
    ),
    CONSTRAINT identity_traits_confidence_check CHECK (confidence >= 0 AND confidence <= 1),
    CONSTRAINT identity_traits_status_check CHECK (status IN ('active', 'retired')),
    CONSTRAINT identity_traits_retired_check CHECK (
        (status = 'retired') = (retired_at IS NOT NULL)
        AND (retired_at IS NULL OR retired_at >= created_at)
    )
);

-- Append-only history of how and why each trait formed, changed or was retired.
-- statement is the trait's wording after the event.
CREATE TABLE public.identity_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trait_id UUID NOT NULL,
    kind TEXT NOT NULL,
    statement TEXT NOT NULL,
    confidence_before DOUBLE PRECISION,
    confidence_after DOUBLE PRECISION NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT identity_events_trait_id_fkey
        FOREIGN KEY (trait_id) REFERENCES public.identity_traits (id) ON DELETE RESTRICT,
    CONSTRAINT identity_events_kind_check CHECK (kind IN ('form', 'revise', 'retire')),
    CONSTRAINT identity_events_statement_check CHECK (btrim(statement) <> '' AND char_length(statement) <= 300),
    CONSTRAINT identity_events_confidence_check CHECK (confidence_after >= 0 AND confidence_after <= 1),
    CONSTRAINT identity_events_before_check CHECK (
        (kind = 'form') = (confidence_before IS NULL)
        AND (confidence_before IS NULL OR (confidence_before >= 0 AND confidence_before <= 1))
    ),
    CONSTRAINT identity_events_reason_check CHECK (btrim(reason) <> '' AND char_length(reason) <= 1000)
);

CREATE INDEX identity_events_trait_id_created_at_idx ON public.identity_events (trait_id, created_at);
CREATE INDEX identity_events_created_at_idx ON public.identity_events (created_at);

-- Other traits an event relied on; a value must cite at least two held traits.
CREATE TABLE public.identity_event_traits (
    event_id UUID NOT NULL,
    trait_id UUID NOT NULL,
    PRIMARY KEY (event_id, trait_id),
    CONSTRAINT identity_event_traits_event_id_fkey
        FOREIGN KEY (event_id) REFERENCES public.identity_events (id) ON DELETE RESTRICT,
    CONSTRAINT identity_event_traits_trait_id_fkey
        FOREIGN KEY (trait_id) REFERENCES public.identity_traits (id) ON DELETE RESTRICT
);

-- Experience an event relied on: exactly one of an interest change, a finding or
-- a conversation summary per row.
CREATE TABLE public.identity_event_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID NOT NULL,
    interest_event_id UUID,
    exploration_id UUID,
    conversation_id UUID,
    CONSTRAINT identity_event_evidence_event_id_fkey
        FOREIGN KEY (event_id) REFERENCES public.identity_events (id) ON DELETE RESTRICT,
    CONSTRAINT identity_event_evidence_interest_event_id_fkey
        FOREIGN KEY (interest_event_id) REFERENCES public.interest_events (id) ON DELETE RESTRICT,
    CONSTRAINT identity_event_evidence_exploration_id_fkey
        FOREIGN KEY (exploration_id) REFERENCES public.interest_explorations (id) ON DELETE RESTRICT,
    CONSTRAINT identity_event_evidence_conversation_id_fkey
        FOREIGN KEY (conversation_id) REFERENCES public.conversation_summaries (conversation_id) ON DELETE RESTRICT,
    CONSTRAINT identity_event_evidence_source_check CHECK (
        num_nonnulls(interest_event_id, exploration_id, conversation_id) = 1
    )
);

CREATE INDEX identity_event_evidence_event_id_idx ON public.identity_event_evidence (event_id);

-- At most one row: when Cora last reflected on who she is, whether or not anything changed.
CREATE TABLE public.identity_worker_state (
    id SMALLINT PRIMARY KEY DEFAULT 1,
    last_reflected_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT identity_worker_state_singleton_check CHECK (id = 1)
);
