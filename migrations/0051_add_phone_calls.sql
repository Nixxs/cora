-- Phone calls (Settings > Services > Phone calls): whether Twilio's call requests are answered. The
-- listener runs when webhooks, the web interface or phone calls are on. Twilio's auth token is kept
-- in the system credential store, never here.
ALTER TABLE public.webhook_settings
    ADD COLUMN phone_enabled BOOLEAN NOT NULL DEFAULT false;
