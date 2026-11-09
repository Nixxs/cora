-- Models of their own for contacts and tasks, exploring, and looking at images.
ALTER TABLE public.model_settings
    DROP CONSTRAINT model_settings_area_check,
    ADD CONSTRAINT model_settings_area_check
        CHECK (area IN ('delegation', 'memory', 'subconscious', 'utility', 'browser', 'contacts', 'exploring', 'images'));
