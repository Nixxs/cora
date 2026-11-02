-- Cora can now pursue a desire she holds: a third kind of exploration attempt, 'pursuit',
-- tied to the desire (desire_id) instead of an interest, sharing the exploration limits.
-- topic: the desire's wish when the step began, so it reads on its own like a wander's topic
-- (up to 300 characters, as a wish is). Its finding is what she did or found toward it, and
-- next_questions are her next steps.
ALTER TABLE public.interest_explorations
    ADD COLUMN desire_id UUID,
    ADD CONSTRAINT interest_explorations_desire_id_fkey
        FOREIGN KEY (desire_id) REFERENCES public.desires (id) ON DELETE RESTRICT,
    DROP CONSTRAINT interest_explorations_kind_check,
    ADD CONSTRAINT interest_explorations_kind_check CHECK (
        kind IN ('interest', 'wander', 'pursuit')
        AND (kind = 'interest') = (interest_id IS NOT NULL)
        AND (kind = 'pursuit') = (desire_id IS NOT NULL)
    ),
    DROP CONSTRAINT interest_explorations_topic_check,
    ADD CONSTRAINT interest_explorations_topic_check CHECK (
        (kind <> 'interest' OR topic IS NULL)
        AND (kind = 'wander' OR wandered_from IS NULL)
        AND (topic IS NULL OR (
            btrim(topic) <> '' AND char_length(topic) <= CASE WHEN kind = 'pursuit' THEN 300 ELSE 200 END
        ))
        AND (kind = 'interest' OR status <> 'found' OR topic IS NOT NULL)
        AND (kind <> 'pursuit' OR topic IS NOT NULL)
    );

CREATE INDEX interest_explorations_desire_id_started_at_idx
    ON public.interest_explorations (desire_id, started_at)
    WHERE desire_id IS NOT NULL;
