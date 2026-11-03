-- Chances Cora noticed for a desire she holds, from her coming week or mail worth knowing (a free
-- Saturday for the trip she'd like to take with her person). Each waits for a quiet moment in a
-- conversation and is offered to her once (offered_at), to suggest lightly or leave unsaid.
CREATE TABLE public.desire_opportunities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    desire_id UUID NOT NULL,
    opportunity TEXT NOT NULL,
    found_at TIMESTAMPTZ NOT NULL,
    offered_at TIMESTAMPTZ,
    CONSTRAINT desire_opportunities_desire_id_fkey
        FOREIGN KEY (desire_id) REFERENCES public.desires (id) ON DELETE RESTRICT,
    CONSTRAINT desire_opportunities_opportunity_check
        CHECK (btrim(opportunity) <> '' AND char_length(opportunity) <= 500)
);

CREATE INDEX desire_opportunities_desire_id_found_at_idx
    ON public.desire_opportunities (desire_id, found_at);
