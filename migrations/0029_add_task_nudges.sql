-- When Cora last nudged a party who had gone quiet, so the hourly check waits a day from then.
ALTER TABLE public.task_parties ADD COLUMN nudged_at TIMESTAMPTZ;
