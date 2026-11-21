-- The dashboard: its open boxes in the order they flow, then its minimised ones
-- ("open,open|minimised,minimised"), replacing the list of minimised boxes alone. A saved list
-- carries over as all minimised; the boxes it doesn't name open in their first order.
ALTER TABLE public.app_settings
    DROP CONSTRAINT app_settings_key_check;

UPDATE public.app_settings
    SET key = 'dashboard_boxes', value = '|' || value
    WHERE key = 'dashboard_minimised';

ALTER TABLE public.app_settings
    ADD CONSTRAINT app_settings_key_check CHECK (
        key IN ('voice', 'spend_project', 'start_mode', 'idle_minutes', 'standby_farewell', 'wake_word',
                'conversation_gap', 'backup_copy_dir', 'twilio_region', 'game_push_to_talk', 'game_chat_channel',
                'game_transcription', 'game_vocabulary', 'game_watch', 'game_chattiness',
                'claude_code_listen', 'claude_code_port', 'claude_code_tell', 'claude_code_transcripts',
                'claude_code_run', 'claude_code_folders', 'local_model_name', 'local_model_idle',
                'local_model_consult', 'sensibo', 'her_room', 'dashboard_boxes')
    );
