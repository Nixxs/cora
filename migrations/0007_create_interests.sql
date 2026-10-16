-- Cora's own interests, kept separate from memories about the user.
-- Strength is a snapshot at strength_at; decay is computed by application code.
CREATE TABLE public.interests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    why_emerged TEXT NOT NULL,
    strength DOUBLE PRECISION NOT NULL,
    strength_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    parent_interest_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_reinforced_at TIMESTAMPTZ,
    CONSTRAINT interests_parent_interest_id_fkey
        FOREIGN KEY (parent_interest_id) REFERENCES public.interests (id) ON DELETE RESTRICT,
    CONSTRAINT interests_title_check CHECK (btrim(title) <> '' AND char_length(title) <= 200),
    CONSTRAINT interests_description_check CHECK (btrim(description) <> '' AND char_length(description) <= 1000),
    CONSTRAINT interests_why_emerged_check CHECK (btrim(why_emerged) <> '' AND char_length(why_emerged) <= 1000),
    CONSTRAINT interests_strength_check CHECK (strength >= 0 AND strength <= 1),
    CONSTRAINT interests_status_check CHECK (status IN ('emerging', 'active', 'dormant')),
    CONSTRAINT interests_parent_check CHECK (parent_interest_id IS NULL OR parent_interest_id <> id)
);

-- Append-only history of how and why each interest changed.
CREATE TABLE public.interest_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    interest_id UUID NOT NULL,
    kind TEXT NOT NULL,
    strength_before DOUBLE PRECISION,
    strength_after DOUBLE PRECISION NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT interest_events_interest_id_fkey
        FOREIGN KEY (interest_id) REFERENCES public.interests (id) ON DELETE RESTRICT,
    CONSTRAINT interest_events_kind_check CHECK (kind IN ('spawn', 'reinforce', 'fade', 'revive')),
    CONSTRAINT interest_events_strength_check CHECK (strength_after >= 0 AND strength_after <= 1),
    CONSTRAINT interest_events_before_check CHECK (
        (kind = 'spawn') = (strength_before IS NULL)
        AND (strength_before IS NULL OR (strength_before >= 0 AND strength_before <= 1))
    ),
    CONSTRAINT interest_events_reason_check CHECK (btrim(reason) <> '' AND char_length(reason) <= 1000)
);

CREATE INDEX interest_events_interest_id_created_at_idx
    ON public.interest_events (interest_id, created_at);

-- Memories that informed an event. Forgetting a memory drops its link; the event
-- and its reason stay.
CREATE TABLE public.interest_event_memories (
    event_id UUID NOT NULL,
    memory_id UUID NOT NULL,
    PRIMARY KEY (event_id, memory_id),
    CONSTRAINT interest_event_memories_event_id_fkey
        FOREIGN KEY (event_id) REFERENCES public.interest_events (id) ON DELETE RESTRICT,
    CONSTRAINT interest_event_memories_memory_id_fkey
        FOREIGN KEY (memory_id) REFERENCES public.memories (id) ON DELETE CASCADE
);

-- At most one row: what the interests worker has already processed.
CREATE TABLE public.interest_worker_state (
    id SMALLINT PRIMARY KEY DEFAULT 1,
    memory_cursor_created_at TIMESTAMPTZ,
    memory_cursor_id UUID,
    last_evolved_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT interest_worker_state_singleton_check CHECK (id = 1),
    CONSTRAINT interest_worker_state_cursor_check CHECK (
        (memory_cursor_created_at IS NULL) = (memory_cursor_id IS NULL)
    )
);
