-- Settings > Game: how often she reacts to what she sees in the game ('off', 'quiet', 'normal' or
-- 'chatty'; normal without a row).
ALTER TABLE public.app_settings
    DROP CONSTRAINT app_settings_key_check,
    ADD CONSTRAINT app_settings_key_check CHECK (
        key IN ('voice', 'spend_project', 'start_mode', 'idle_minutes', 'standby_farewell', 'wake_word',
                'conversation_gap', 'backup_copy_dir', 'twilio_region', 'game_push_to_talk', 'game_chat_channel',
                'game_transcription', 'game_vocabulary', 'game_watch', 'game_chattiness')
    );
