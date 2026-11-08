-- A model of its own for browsing: her browser helper and her sign-ups.
ALTER TABLE public.model_settings
    DROP CONSTRAINT model_settings_area_check,
    ADD CONSTRAINT model_settings_area_check
        CHECK (area IN ('delegation', 'memory', 'subconscious', 'utility', 'browser'));
