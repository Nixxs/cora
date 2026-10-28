-- How a client treated Cora in each finished conversation, rated with her impression of them:
-- 'fine', 'rude', 'abusive' or 'threatening', with a short note in her own words (never quotes)
-- for anything but fine. The person gets a heads-up about anything but fine; reported_at is when
-- it went to them. Conversations rated before this migration have no rating and are never reported.
ALTER TABLE public.impression_updates
    ADD COLUMN conduct TEXT,
    ADD COLUMN conduct_note TEXT,
    ADD COLUMN reported_at TIMESTAMPTZ,
    ADD CONSTRAINT impression_updates_conduct_check CHECK (
        conduct IS NULL OR conduct IN ('fine', 'rude', 'abusive', 'threatening')
    ),
    ADD CONSTRAINT impression_updates_conduct_note_check CHECK (
        (conduct_note IS NULL) = (conduct IS NULL OR conduct = 'fine')
        AND (conduct_note IS NULL OR (btrim(conduct_note) <> '' AND char_length(conduct_note) <= 500))
    );
