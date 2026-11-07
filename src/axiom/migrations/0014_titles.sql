-- A title is what a person reads a memory by: a few words in the user's
-- language, next to the slug the AI addresses it by. Memories written before
-- titles existed keep an empty one, and the pages fall back to the
-- description until an AI rewrites them.

ALTER TABLE memories ADD COLUMN title TEXT NOT NULL DEFAULT '';
