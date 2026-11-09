-- The Twilio region for calls she places, from Settings > Services > Phone calls, replacing .env's
-- CORA_TWILIO_API: 'au1', 'us1' or 'ie1'. Without a row, .env's value (or Sydney, AU1) is used.
ALTER TABLE public.app_settings
    DROP CONSTRAINT app_settings_key_check,
    ADD CONSTRAINT app_settings_key_check CHECK (
        key IN ('voice', 'spend_project', 'start_mode', 'idle_minutes', 'standby_farewell', 'wake_word',
                'conversation_gap', 'backup_copy_dir', 'twilio_region')
    );
