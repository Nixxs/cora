-- When Cora was offered a finding to raise on her own. Set once, before it is sent
-- to Live, so the same finding is never offered twice even if sending fails.
ALTER TABLE public.interest_explorations
    ADD COLUMN mentioned_at TIMESTAMPTZ,
    ADD CONSTRAINT interest_explorations_mentioned_check CHECK (mentioned_at IS NULL OR status = 'found');
