-- Phone calls from contacts (Settings > Signal, each person's Calls switch): whether someone the
-- person added may reach Cora by phone. Off unless the person turns it on for them.
ALTER TABLE public.people
    ADD COLUMN may_call BOOLEAN NOT NULL DEFAULT false;
