-- Messages can also come and go over Cora's own Signal account.
ALTER TABLE public.messages
    DROP CONSTRAINT messages_channel_check,
    ADD CONSTRAINT messages_channel_check CHECK (channel IN ('voice', 'keyboard', 'screen', 'signal'));
