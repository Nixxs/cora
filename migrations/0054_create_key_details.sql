-- Key details: things the person has told Cora that she should always have to hand (their email,
-- phone, address, their family's contact details...), each a free-form label and its value. All of
-- them reach her prompts for the person's own chats, never a client's.
CREATE TABLE public.key_details (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT key_details_label_check CHECK (
        label = btrim(label) AND label <> '' AND char_length(label) <= 100
    ),
    CONSTRAINT key_details_value_check CHECK (
        value = btrim(value) AND value <> '' AND char_length(value) <= 500
    )
);

-- One entry per label, whatever its case: saving "Wife's phone" again replaces it.
CREATE UNIQUE INDEX key_details_label_key ON public.key_details (lower(label));
