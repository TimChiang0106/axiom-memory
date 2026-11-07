-- An archived memory is kept as history — superseded, closed, no longer in
-- force — rather than forgotten. The library hides it unless asked. Before
-- this column the AI clients marked such memories by opening the title or
-- description with （歷史…）, so those are archived here.

ALTER TABLE memories ADD COLUMN archived BOOLEAN NOT NULL DEFAULT false;

UPDATE memories SET archived = true
WHERE title ~ '^\s*[（(]歷史' OR description ~ '^\s*[（(]歷史';
