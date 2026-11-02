-- Sites Cora asked to sign up to, for an account of her own: what it is and why she wants it,
-- the person's answer, and how signing up went. One account per site: a sign-up under way,
-- waiting for its confirmation email or done holds the site.
CREATE TABLE public.sign_ups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site TEXT NOT NULL,
    url TEXT NOT NULL,
    what TEXT NOT NULL,
    why TEXT NOT NULL,
    status TEXT NOT NULL,
    blocked_by TEXT,
    note TEXT,
    asked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMPTZ,
    CONSTRAINT sign_ups_site_check CHECK (btrim(site) <> '' AND char_length(site) <= 253),
    CONSTRAINT sign_ups_url_check CHECK (url LIKE 'http%' AND char_length(url) <= 2000),
    CONSTRAINT sign_ups_what_check CHECK (btrim(what) <> '' AND char_length(what) <= 300),
    CONSTRAINT sign_ups_why_check CHECK (btrim(why) <> '' AND char_length(why) <= 500),
    CONSTRAINT sign_ups_status_check CHECK (status IN (
        'declined', 'timed_out', 'signing_up', 'confirm_email', 'done', 'blocked', 'failed'
    )),
    CONSTRAINT sign_ups_blocked_by_check CHECK (
        (blocked_by IS NOT NULL) = (status = 'blocked')
        AND (blocked_by IS NULL OR blocked_by IN ('captcha', 'phone', 'payment', 'personal_details', 'other'))
    ),
    CONSTRAINT sign_ups_note_check CHECK (note IS NULL OR char_length(note) <= 1000)
);

CREATE UNIQUE INDEX sign_ups_one_account_per_site_idx ON public.sign_ups (site)
    WHERE status IN ('signing_up', 'confirm_email', 'done');
CREATE INDEX sign_ups_asked_at_idx ON public.sign_ups (asked_at DESC);
