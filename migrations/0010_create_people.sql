-- The people Cora talks with. display_name stays NULL until she is told it.
CREATE TABLE public.people (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name TEXT,
    first_met_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT people_display_name_check CHECK (
        display_name IS NULL
        OR (display_name = btrim(display_name) AND display_name <> '' AND char_length(display_name) <= 100)
    )
);

-- An install with history already knows its person as Nick, the name the prompts
-- used until now. A fresh install has no conversations, so nobody is seeded and
-- Cora starts out meeting a stranger.
INSERT INTO public.people (display_name, first_met_at)
SELECT 'Nick', min(started_at) FROM public.conversations
HAVING count(*) > 0;
