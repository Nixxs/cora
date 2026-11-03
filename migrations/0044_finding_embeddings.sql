-- A finding's embedding (text-embedding-3-small, of its title and text), made the first time the
-- relevance check needs it, so Cora can raise a finding when the conversation touches on it.
ALTER TABLE public.interest_explorations
    ADD COLUMN finding_embedding vector(1536),
    ADD CONSTRAINT interest_explorations_finding_embedding_check CHECK (
        finding_embedding IS NULL OR (status = 'found' AND vector_norm(finding_embedding) > 0)
    );
