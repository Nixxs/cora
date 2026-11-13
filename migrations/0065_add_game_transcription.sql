-- Settings > Game: how push-to-talk turns speech into text ('openai' or 'local'; OpenAI without a
-- row), and the person's own names and words that steer its spelling (none without a row).
ALTER TABLE public.app_settings
    DROP CONSTRAINT app_settings_key_check,
    ADD CONSTRAINT app_settings_key_check CHECK (
        key IN ('voice', 'spend_project', 'start_mode', 'idle_minutes', 'standby_farewell', 'wake_word',
                'conversation_gap', 'backup_copy_dir', 'twilio_region', 'game_push_to_talk', 'game_chat_channel',
                'game_transcription', 'game_vocabulary')
    );
