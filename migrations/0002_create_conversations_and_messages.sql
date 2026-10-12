-- Logical conversations are independent of model transport connections.
CREATE TABLE public.conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'active',
    CONSTRAINT conversations_status_check
        CHECK (status IN ('active', 'closed', 'interrupted')),
    CONSTRAINT conversations_times_check
        CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE TABLE public.messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL,
    sequence_number INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    channel TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'incomplete',
    delivery_status TEXT NOT NULL DEFAULT 'not_applicable',
    CONSTRAINT messages_conversation_id_fkey
        FOREIGN KEY (conversation_id) REFERENCES public.conversations (id) ON DELETE RESTRICT,
    CONSTRAINT messages_conversation_sequence_key UNIQUE (conversation_id, sequence_number),
    CONSTRAINT messages_sequence_check CHECK (sequence_number > 0),
    CONSTRAINT messages_role_check CHECK (role IN ('user', 'assistant')),
    CONSTRAINT messages_channel_check CHECK (channel IN ('voice', 'keyboard', 'screen')),
    CONSTRAINT messages_status_check CHECK (status IN ('incomplete', 'completed', 'interrupted')),
    CONSTRAINT messages_delivery_status_check
        CHECK (delivery_status IN ('not_applicable', 'pending', 'sent', 'failed')),
    CONSTRAINT messages_text_check CHECK (btrim(text) <> '')
);