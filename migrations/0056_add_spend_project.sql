-- Whose OpenAI spend the dashboard shows (a project's id, or 'all'); without a row it's automatic:
-- the project named for Cora once it has spend, else the whole organisation.
ALTER TABLE public.app_settings
    DROP CONSTRAINT app_settings_key_check,
    ADD CONSTRAINT app_settings_key_check CHECK (key IN ('voice', 'spend_project'));
