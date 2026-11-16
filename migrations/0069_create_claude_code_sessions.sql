-- The last Claude Code session Cora ran in each project folder, so "continue" carries on her own
-- session after she restarts. The folder is kept case-folded (Windows paths ignore case). Rows go
-- after 30 days unused, as Claude Code clears its transcripts by default.
CREATE TABLE public.claude_code_sessions (
    folder TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ran_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT claude_code_sessions_folder_check CHECK (btrim(folder) <> '' AND char_length(folder) <= 1000),
    CONSTRAINT claude_code_sessions_session_id_check CHECK (session_id ~ '^[A-Za-z0-9_-]{1,100}$')
);
