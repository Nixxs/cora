-- How one finished conversation went between Cora and the person, judged once
-- from its original messages. Relationship state is derived from these later;
-- they are never edited or deleted, so a relationship reset keeps them.
CREATE TABLE public.interaction_appraisals (
    conversation_id UUID PRIMARY KEY,
    person_id UUID NOT NULL,
    -- 'development' marks testing or debugging Cora herself; it never shapes the relationship.
    setting TEXT NOT NULL,
    -- How the person treated Cora overall.
    tone TEXT NOT NULL,
    -- Frustration at a bug or situation is not the same as mistreatment aimed at her.
    friction TEXT NOT NULL,
    -- Whether Cora got something wrong that affected the person.
    cora_mistake BOOLEAN NOT NULL,
    -- Who made amends: the person for friction aimed at her, Cora for her mistake.
    repair TEXT NOT NULL,
    reason TEXT NOT NULL,
    appraised_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT interaction_appraisals_conversation_id_fkey
        FOREIGN KEY (conversation_id) REFERENCES public.conversations (id) ON DELETE RESTRICT,
    CONSTRAINT interaction_appraisals_person_id_fkey
        FOREIGN KEY (person_id) REFERENCES public.people (id) ON DELETE RESTRICT,
    CONSTRAINT interaction_appraisals_setting_check CHECK (setting IN ('everyday', 'development')),
    CONSTRAINT interaction_appraisals_tone_check CHECK (tone IN ('warm', 'neutral', 'curt', 'hostile')),
    CONSTRAINT interaction_appraisals_friction_check CHECK (friction IN ('none', 'at_situation', 'at_cora')),
    CONSTRAINT interaction_appraisals_repair_check CHECK (
        repair IN ('none', 'person', 'cora', 'both')
        AND (repair NOT IN ('person', 'both') OR friction = 'at_cora')
        AND (repair NOT IN ('cora', 'both') OR cora_mistake)
    ),
    CONSTRAINT interaction_appraisals_reason_check CHECK (btrim(reason) <> '' AND char_length(reason) <= 1000)
);

CREATE INDEX interaction_appraisals_person_id_appraised_at_idx
    ON public.interaction_appraisals (person_id, appraised_at);
