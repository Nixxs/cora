-- Small, evidence-linked memories; extraction and recall are separate steps.
CREATE TABLE public.memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_message_id UUID NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    evidence TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT memories_source_message_id_fkey
        FOREIGN KEY (source_message_id) REFERENCES public.messages (id) ON DELETE RESTRICT,
    CONSTRAINT memories_source_kind_text_key UNIQUE (source_message_id, kind, text),
    CONSTRAINT memories_kind_check CHECK (kind IN ('fact', 'preference', 'decision', 'task')),
    CONSTRAINT memories_text_check CHECK (btrim(text) <> '' AND char_length(text) <= 500),
    CONSTRAINT memories_evidence_check CHECK (btrim(evidence) <> '')
);