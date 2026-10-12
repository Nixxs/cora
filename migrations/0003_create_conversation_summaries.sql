-- One summary per conversation; generation is a separate application step.
CREATE TABLE public.conversation_summaries (
    conversation_id UUID PRIMARY KEY,
    text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT conversation_summaries_conversation_id_fkey
        FOREIGN KEY (conversation_id) REFERENCES public.conversations (id) ON DELETE RESTRICT,
    CONSTRAINT conversation_summaries_text_check CHECK (btrim(text) <> '')
);