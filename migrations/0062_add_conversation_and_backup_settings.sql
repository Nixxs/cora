-- From Settings > General, replacing .env: minutes of quiet that end a conversation (in standby,
-- and contacts' chats), and a second folder every backup is copied to (an absolute path, or
-- 'none' for no copy). Without a row, .env's value (or the default) is used.
ALTER TABLE public.app_settings
    DROP CONSTRAINT app_settings_key_check,
    ADD CONSTRAINT app_settings_key_check CHECK (
        key IN ('voice', 'spend_project', 'start_mode', 'idle_minutes', 'standby_farewell', 'wake_word',
                'conversation_gap', 'backup_copy_dir')
    );
