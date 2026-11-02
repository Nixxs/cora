-- stalled_told_at: when she told the person a sign-up's confirmation email never came (once).
ALTER TABLE public.sign_ups
    ADD COLUMN stalled_told_at TIMESTAMPTZ;

-- A site she came across while exploring and would like an account on: its address, what it is
-- and why she wants it, all or none. sign_up_asked_at: when she took it to the person (once).
ALTER TABLE public.interest_explorations
    ADD COLUMN sign_up_url TEXT,
    ADD COLUMN sign_up_what TEXT,
    ADD COLUMN sign_up_why TEXT,
    ADD COLUMN sign_up_asked_at TIMESTAMPTZ,
    ADD CONSTRAINT interest_explorations_sign_up_check CHECK (
        (sign_up_url IS NULL) = (sign_up_what IS NULL) AND (sign_up_url IS NULL) = (sign_up_why IS NULL)
        AND (sign_up_url IS NULL OR (
            sign_up_url LIKE 'http%' AND char_length(sign_up_url) <= 2000
            AND btrim(sign_up_what) <> '' AND char_length(sign_up_what) <= 300
            AND btrim(sign_up_why) <> '' AND char_length(sign_up_why) <= 500
        ))
    );
