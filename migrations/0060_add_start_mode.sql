-- How Cora starts when the app opens: 'active' (connected to Live) or 'standby' (waiting for
-- the wake word or a message). Without a row she starts active.
ALTER TABLE public.app_settings
    DROP CONSTRAINT app_settings_key_check,
    ADD CONSTRAINT app_settings_key_check CHECK (key IN ('voice', 'spend_project', 'start_mode'));
