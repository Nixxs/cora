-- A feeling's reason is kept whole (up to 1,200 characters) for the dashboard; her context still
-- shows the first 200. Reasons saved before this were already cut to 200.
ALTER TABLE public.feeling_events
    DROP CONSTRAINT feeling_events_reason_check,
    ADD CONSTRAINT feeling_events_reason_check CHECK (btrim(reason) <> '' AND char_length(reason) <= 1200);
