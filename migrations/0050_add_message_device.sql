-- Which paired device a web message was sent from ("iPhone", "iPad (Home Screen app)"), so the
-- page and the desk can say so. Empty for every other channel and for her replies.
ALTER TABLE public.messages
    ADD COLUMN device TEXT,
    ADD CONSTRAINT messages_device_check CHECK (device IS NULL OR (btrim(device) <> '' AND char_length(device) <= 60));
