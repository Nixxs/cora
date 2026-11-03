-- Cora's feelings, as the reactions she reported (append-only). Each feeling's level now is worked
-- out from these and the fading in subconscious/emotions/feeling_rules.py, which also keeps the
-- history for the dashboard. conversation_id is empty for a reaction outside a conversation.
CREATE TABLE public.feeling_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    feeling TEXT NOT NULL,
    strength TEXT NOT NULL,
    reason TEXT NOT NULL,
    felt_at TIMESTAMPTZ NOT NULL,
    conversation_id UUID,
    CONSTRAINT feeling_events_conversation_id_fkey
        FOREIGN KEY (conversation_id) REFERENCES public.conversations (id) ON DELETE RESTRICT,
    CONSTRAINT feeling_events_feeling_check CHECK (
        feeling IN ('excitement', 'contentment', 'curiosity', 'fulfilment', 'irritation', 'hurt')
    ),
    CONSTRAINT feeling_events_strength_check CHECK (strength IN ('small', 'medium', 'strong')),
    CONSTRAINT feeling_events_reason_check CHECK (btrim(reason) <> '' AND char_length(reason) <= 200)
);

CREATE INDEX feeling_events_felt_at_idx ON public.feeling_events (felt_at);
CREATE INDEX feeling_events_conversation_id_idx ON public.feeling_events (conversation_id);
