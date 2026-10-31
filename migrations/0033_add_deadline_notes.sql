-- Task deadlines (T5b): when she told the person a deadline was a day away, and when she told
-- them it had passed, so each is said once.
ALTER TABLE public.tasks
    ADD COLUMN deadline_warned_at TIMESTAMPTZ,
    ADD COLUMN deadline_passed_at TIMESTAMPTZ;
