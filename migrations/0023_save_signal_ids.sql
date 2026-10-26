-- Each person's Signal ID (uuid), saved once learned. Signal can hide someone's number
-- ("who can find me / see my number: Nobody"); the ID still identifies them, so Cora can
-- recognise and message people by it after a restart whatever their privacy settings.
ALTER TABLE public.people
    ADD COLUMN signal_id TEXT,
    ADD CONSTRAINT people_signal_id_key UNIQUE (signal_id);

-- The same for the primary person's own number.
ALTER TABLE public.signal_settings
    ADD COLUMN allowed_signal_id TEXT;
