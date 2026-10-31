-- Email parties: someone Cora emails for a task from her own mailbox, such as a business she
-- found. They aren't contacts, so each row carries who they are (name, as she knows them) and the
-- email thread (Microsoft Graph conversationId) her messages with them belong to.
ALTER TABLE public.task_parties
    ADD COLUMN name TEXT,
    ADD COLUMN thread_id TEXT,
    ADD CONSTRAINT task_parties_name_check CHECK (name IS NULL OR (btrim(name) <> '' AND char_length(name) <= 100)),
    ADD CONSTRAINT task_parties_thread_check CHECK (thread_id IS NULL OR char_length(thread_id) <= 500),
    ADD CONSTRAINT task_parties_address_key UNIQUE (task_id, address);
