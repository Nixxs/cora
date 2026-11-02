-- What happens to her desires (coming to really want one, starting to pursue it, the person's
-- 'not now', fulfilling it, letting it go or it fading) is an experience of her own: the identity
-- and desire reflections can cite it, so pursuing and fulfilling desires shapes who she is and
-- what she wants next.
ALTER TABLE public.identity_event_evidence
    ADD COLUMN desire_event_id UUID,
    ADD CONSTRAINT identity_event_evidence_desire_event_id_fkey
        FOREIGN KEY (desire_event_id) REFERENCES public.desire_events (id) ON DELETE RESTRICT,
    DROP CONSTRAINT identity_event_evidence_source_check,
    ADD CONSTRAINT identity_event_evidence_source_check CHECK (
        num_nonnulls(interest_event_id, exploration_id, conversation_id, desire_event_id) = 1
    );

ALTER TABLE public.desire_sources
    ADD COLUMN desire_event_id UUID,
    ADD CONSTRAINT desire_sources_desire_event_id_fkey
        FOREIGN KEY (desire_event_id) REFERENCES public.desire_events (id) ON DELETE RESTRICT,
    DROP CONSTRAINT desire_sources_source_check,
    ADD CONSTRAINT desire_sources_source_check CHECK (
        num_nonnulls(interest_id, exploration_id, conversation_id, desire_event_id) = 1
    );
