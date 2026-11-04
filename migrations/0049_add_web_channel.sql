-- The person can also write to Cora from her web page; those messages and her replies join their
-- one conversation, like Signal and email.
ALTER TABLE public.messages
    DROP CONSTRAINT messages_channel_check,
    ADD CONSTRAINT messages_channel_check CHECK (channel IN ('voice', 'keyboard', 'screen', 'signal', 'email', 'web'));
