-- How Cora pictures herself: images she chose to represent her (she isn't human), with her
-- own description of each. 'self' is how she sees herself; 'work' is her at work. Kept apart
-- from identity traits, so reflection never retires it, but stored rather than fixed in code,
-- so it can change if she ever wants. The images are kept here so backups include them.
CREATE TABLE public.self_image (
    kind TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    image BYTEA,
    image_type TEXT,
    chosen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT self_image_kind_check CHECK (kind IN ('self', 'work')),
    CONSTRAINT self_image_description_check CHECK (btrim(description) <> '' AND char_length(description) <= 1000),
    CONSTRAINT self_image_image_check CHECK ((image IS NULL) = (image_type IS NULL))
);
