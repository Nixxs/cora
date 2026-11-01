-- Cora can now wander: an exploration not tied to any interest, starting from one of her
-- tangents or from an area picked at random. kind: 'interest' (as every earlier attempt was)
-- or 'wander', which has no interest. topic: what a wander was about, known at the start when
-- she follows a tangent, otherwise chosen by her. wandered_from: the exploration whose tangent
-- she followed.
--
-- reach_out / reach_out_why: a short message for the person when something she found really
-- excited her or she wants their human perspective; reached_out_at: when it was sent.
ALTER TABLE public.interest_explorations
    ALTER COLUMN interest_id DROP NOT NULL,
    ADD COLUMN kind TEXT NOT NULL DEFAULT 'interest',
    ADD COLUMN topic TEXT,
    ADD COLUMN wandered_from UUID,
    ADD COLUMN reach_out TEXT,
    ADD COLUMN reach_out_why TEXT,
    ADD COLUMN reached_out_at TIMESTAMPTZ,
    ADD CONSTRAINT interest_explorations_wandered_from_fkey
        FOREIGN KEY (wandered_from) REFERENCES public.interest_explorations (id) ON DELETE RESTRICT,
    ADD CONSTRAINT interest_explorations_kind_check CHECK (
        kind IN ('interest', 'wander') AND (kind = 'interest') = (interest_id IS NOT NULL)
    ),
    ADD CONSTRAINT interest_explorations_topic_check CHECK (
        (kind = 'wander' OR (topic IS NULL AND wandered_from IS NULL))
        AND (topic IS NULL OR (btrim(topic) <> '' AND char_length(topic) <= 200))
        AND (kind = 'interest' OR status <> 'found' OR topic IS NOT NULL)
    ),
    ADD CONSTRAINT interest_explorations_reach_out_check CHECK (
        (reach_out IS NULL) = (reach_out_why IS NULL)
        AND (reach_out IS NULL OR (
            status = 'found' AND btrim(reach_out) <> '' AND char_length(reach_out) <= 600
            AND btrim(reach_out_why) <> '' AND char_length(reach_out_why) <= 500
        ))
        AND (reached_out_at IS NULL OR reach_out IS NOT NULL)
    );

-- Wanders feed the interest cycle as sparks even without tangents.
DROP INDEX public.interest_explorations_unused_sparks_idx;
CREATE INDEX interest_explorations_unused_sparks_idx
    ON public.interest_explorations (finished_at)
    WHERE sparks_used_at IS NULL AND (jsonb_array_length(tangents) > 0 OR kind = 'wander');

CREATE INDEX interest_explorations_reached_out_at_idx
    ON public.interest_explorations (reached_out_at)
    WHERE reached_out_at IS NOT NULL;
