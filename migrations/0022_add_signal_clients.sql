-- Other people Cora may talk with over Signal, as clients. The person she works with
-- stays the one 'primary' person; everyone added in Settings is a 'client' with a
-- name and the Signal number she answers.
ALTER TABLE public.people
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'primary',
    ADD COLUMN signal_number TEXT,
    ADD CONSTRAINT people_kind_check CHECK (kind IN ('primary', 'client')),
    ADD CONSTRAINT people_client_named_check CHECK (kind = 'primary' OR display_name IS NOT NULL),
    ADD CONSTRAINT people_signal_number_key UNIQUE (signal_number);

-- Who a conversation was with. NULL means the primary person, as every conversation
-- before this was; a client's chats are their own conversations.
ALTER TABLE public.conversations
    ADD COLUMN person_id UUID REFERENCES public.people (id) ON DELETE RESTRICT;
CREATE INDEX conversations_person_id_idx ON public.conversations (person_id) WHERE person_id IS NOT NULL;
