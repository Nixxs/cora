-- Cora exploring one of her own interests. Every attempt is kept, including failed
-- ones, so exploration limits count attempts rather than only successful findings.
CREATE TABLE public.interest_explorations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    interest_id UUID NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    finding TEXT,
    next_questions TEXT[] NOT NULL,
    sources TEXT[] NOT NULL,
    CONSTRAINT interest_explorations_interest_id_fkey
        FOREIGN KEY (interest_id) REFERENCES public.interests (id) ON DELETE RESTRICT,
    CONSTRAINT interest_explorations_status_check CHECK (status IN ('running', 'found', 'failed')),
    CONSTRAINT interest_explorations_finished_check CHECK (
        (status = 'running') = (finished_at IS NULL)
        AND (finished_at IS NULL OR finished_at >= started_at)
    ),
    CONSTRAINT interest_explorations_finding_check CHECK (
        (status = 'found') = (finding IS NOT NULL)
        AND (finding IS NULL OR (btrim(finding) <> '' AND char_length(finding) <= 2000))
    ),
    CONSTRAINT interest_explorations_next_questions_check CHECK (
        cardinality(next_questions) <= 3 AND (status = 'found' OR cardinality(next_questions) = 0)
    ),
    CONSTRAINT interest_explorations_sources_check CHECK (
        cardinality(sources) <= 10 AND (status = 'found' OR cardinality(sources) = 0)
    )
);

CREATE INDEX interest_explorations_started_at_idx
    ON public.interest_explorations (started_at);

CREATE INDEX interest_explorations_interest_id_started_at_idx
    ON public.interest_explorations (interest_id, started_at);
