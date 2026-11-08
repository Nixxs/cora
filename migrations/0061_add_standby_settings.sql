-- Standby settings from Settings > General, replacing .env: minutes of quiet before automatic
-- standby ('0' for off), her heads-up before it ('on'/'off'), and whether her wake word is on
-- ('on'/'off', from the Wake button). Without a row, .env's value (or the default) is used.
ALTER TABLE public.app_settings
    DROP CONSTRAINT app_settings_key_check,
    ADD CONSTRAINT app_settings_key_check CHECK (
        key IN ('voice', 'spend_project', 'start_mode', 'idle_minutes', 'standby_farewell', 'wake_word')
    );
