-- The person can also email Cora's own address; those emails and her replies join their one conversation.
ALTER TABLE public.messages
    DROP CONSTRAINT messages_channel_check,
    ADD CONSTRAINT messages_channel_check CHECK (channel IN ('voice', 'keyboard', 'screen', 'signal', 'email'));
