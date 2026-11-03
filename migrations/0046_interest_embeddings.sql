-- An interest's embedding (text-embedding-3-small) of its title and latest finding, and the text
-- it was made from, so it is redone when that changes. Used to give a contact's chat only the
-- interests their message touches on.
ALTER TABLE public.interests
    ADD COLUMN embedding vector(1536),
    ADD COLUMN embedded_text TEXT,
    ADD CONSTRAINT interests_embedding_check CHECK (
        (embedding IS NULL) = (embedded_text IS NULL)
        AND (embedding IS NULL OR vector_norm(embedding) > 0)
    );
