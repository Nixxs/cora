-- Calls Cora placed for a task, kept whole: a task step holds only 4,000 characters, which a
-- phone menu and hold messages can fill before anyone answers. "replies" is how quickly she
-- answered on the call (her side of the latency), for tuning.
CREATE TABLE public.task_calls (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES public.tasks(id) ON DELETE CASCADE,
    who TEXT NOT NULL,
    number TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    transcript TEXT NOT NULL,
    replies TEXT,
    CONSTRAINT task_calls_who_check CHECK (btrim(who) <> '' AND char_length(who) <= 320),
    CONSTRAINT task_calls_number_check CHECK (btrim(number) <> '' AND char_length(number) <= 320),
    CONSTRAINT task_calls_transcript_check CHECK (char_length(transcript) <= 100000),
    CONSTRAINT task_calls_replies_check CHECK (replies IS NULL OR char_length(replies) <= 500)
);

CREATE INDEX task_calls_task_id_idx ON public.task_calls (task_id, started_at);
