-- The share check: did the person mean a memory to reach someone Cora talks with as a client
-- ("tell Sarah I'll be late", "anyone who asks, I'm away till Monday")? share_audience is
-- 'anyone', or 'person' with the name they used; NULL means nobody, which is also what an
-- unsure check saves. Only the primary person's memories are checked.
ALTER TABLE public.memories
    ADD COLUMN share_checked_at TIMESTAMPTZ,
    ADD COLUMN share_audience TEXT,
    ADD COLUMN shared_with_name TEXT,
    ADD CONSTRAINT memories_share_check CHECK (
        (share_audience IS NULL AND shared_with_name IS NULL)
        OR (share_audience = 'anyone' AND shared_with_name IS NULL AND share_checked_at IS NOT NULL)
        OR (share_audience = 'person' AND btrim(shared_with_name) <> '' AND share_checked_at IS NOT NULL)
    );
