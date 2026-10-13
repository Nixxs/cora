ALTER TABLE public.memories
    ADD COLUMN embedding vector(1536),
    ADD COLUMN embedding_model TEXT,
    ADD COLUMN embedding_created_at TIMESTAMPTZ,
    ADD CONSTRAINT memories_embedding_metadata_check CHECK (
        (embedding IS NULL AND embedding_model IS NULL AND embedding_created_at IS NULL)
        OR (embedding IS NOT NULL AND embedding_model IS NOT NULL
            AND embedding_model = 'text-embedding-3-small' AND embedding_created_at IS NOT NULL)
    ),
    ADD CONSTRAINT memories_embedding_nonzero_check CHECK (
        embedding IS NULL OR vector_norm(embedding) > 0
    );