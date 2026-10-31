-- browsed: the attempt used Cora's own browser (real pages, followed link by link) instead
-- of web search. Counted so browsing sessions stay within their own daily limit.
ALTER TABLE public.interest_explorations
    ADD COLUMN browsed BOOLEAN NOT NULL DEFAULT false;
