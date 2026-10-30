-- Errands Cora carries out with other people for the person she works with: a goal in their
-- words, the people involved (parties) and a log of the steps taken. Generic throughout: any
-- contact (or, later, email address) is just a party row with its own brief.
CREATE TABLE public.tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',
    result TEXT,
    deadline TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMPTZ,
    CONSTRAINT tasks_goal_check CHECK (btrim(goal) <> '' AND char_length(goal) <= 1000),
    CONSTRAINT tasks_status_check CHECK (status IN ('open', 'waiting', 'needs_person', 'done', 'cancelled')),
    CONSTRAINT tasks_result_check CHECK (result IS NULL OR char_length(result) <= 4000),
    CONSTRAINT tasks_closed_check CHECK ((closed_at IS NOT NULL) = (status IN ('done', 'cancelled')))
);

-- Someone the task needs: who, on which channel, what she should find out or agree with them and
-- what she may share (the brief), and how their part stands. Signal parties are added contacts;
-- email parties (later) are addresses found during the task.
CREATE TABLE public.task_parties (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES public.tasks (id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    person_id UUID REFERENCES public.people (id) ON DELETE CASCADE,
    address TEXT,
    brief TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',
    contacted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_heard_at TIMESTAMPTZ,
    nudges SMALLINT NOT NULL DEFAULT 0,
    outcome TEXT,
    CONSTRAINT task_parties_channel_check CHECK (channel IN ('signal', 'email')),
    CONSTRAINT task_parties_who_check CHECK (
        (channel = 'signal' AND person_id IS NOT NULL AND address IS NULL)
        OR (channel = 'email' AND person_id IS NULL AND btrim(address) <> '' AND char_length(address) <= 320)
    ),
    CONSTRAINT task_parties_brief_check CHECK (btrim(brief) <> '' AND char_length(brief) <= 2000),
    CONSTRAINT task_parties_status_check CHECK (status IN ('waiting', 'done')),
    CONSTRAINT task_parties_nudges_check CHECK (nudges >= 0),
    CONSTRAINT task_parties_outcome_check CHECK (outcome IS NULL OR char_length(outcome) <= 3000),
    CONSTRAINT task_parties_person_key UNIQUE (task_id, person_id)
);
CREATE INDEX task_parties_person_id_idx ON public.task_parties (person_id) WHERE person_id IS NOT NULL;

-- What happened, in order, so the person can see progress and she can pick up after a restart.
CREATE TABLE public.task_steps (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES public.tasks (id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    text TEXT NOT NULL,
    CONSTRAINT task_steps_text_check CHECK (btrim(text) <> '' AND char_length(text) <= 4000)
);
CREATE INDEX task_steps_task_id_idx ON public.task_steps (task_id, id);
