-- Settings > Claude Code: listening for its hooks and on which port, what she tells you about, whether
-- she may read transcripts and run sessions, and the projects folders those live in (a JSON list of
-- paths, so it may be longer than other settings).
ALTER TABLE public.app_settings
    DROP CONSTRAINT app_settings_key_check,
    ADD CONSTRAINT app_settings_key_check CHECK (
        key IN ('voice', 'spend_project', 'start_mode', 'idle_minutes', 'standby_farewell', 'wake_word',
                'conversation_gap', 'backup_copy_dir', 'twilio_region', 'game_push_to_talk', 'game_chat_channel',
                'game_transcription', 'game_vocabulary', 'game_watch', 'game_chattiness',
                'claude_code_listen', 'claude_code_port', 'claude_code_tell', 'claude_code_transcripts',
                'claude_code_run', 'claude_code_folders')
    ),
    DROP CONSTRAINT app_settings_value_check,
    ADD CONSTRAINT app_settings_value_check CHECK (
        btrim(value) <> ''
        AND char_length(value) <= CASE WHEN key = 'claude_code_folders' THEN 2000 ELSE 200 END
    );
