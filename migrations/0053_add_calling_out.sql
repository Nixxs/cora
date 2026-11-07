-- Calling out for tasks (P3): the Twilio account Cora places calls through and her number to call
-- from (Settings > Services > Phone calls), and people she calls for a task as task parties.
ALTER TABLE public.webhook_settings
    ADD COLUMN twilio_account_sid TEXT,
    ADD COLUMN phone_number TEXT;

ALTER TABLE public.task_parties
    DROP CONSTRAINT task_parties_channel_check,
    ADD CONSTRAINT task_parties_channel_check CHECK (channel IN ('signal', 'email', 'phone')),
    DROP CONSTRAINT task_parties_who_check,
    ADD CONSTRAINT task_parties_who_check CHECK (
        (channel = 'signal' AND person_id IS NOT NULL AND address IS NULL)
        OR (channel IN ('email', 'phone') AND person_id IS NULL AND btrim(address) <> ''
            AND char_length(address) <= 320)
    );
