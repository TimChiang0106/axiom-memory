-- Delivery tracking on top of tasks. `status` keeps driving the lists (open,
-- done, expired); a state is the user's own word for where an open task
-- stands, stored as rows so the vocabulary can change without a deploy. Its
-- one piece of behaviour is `waiting`: someone else holds the task, so it
-- cannot be overdue, and planned_date becomes the day to follow up.

CREATE TABLE task_states (
    name     TEXT PRIMARY KEY CHECK (length(name) BETWEEN 1 AND 20),
    waiting  BOOLEAN NOT NULL DEFAULT false,
    position INT NOT NULL DEFAULT 0
);

INSERT INTO task_states (name, waiting, position) VALUES
    ('進行中', false, 1),
    ('遭阻擋', true, 2),
    ('等待中', true, 3),
    ('待驗證', false, 4);

ALTER TABLE tasks
    ADD COLUMN state      TEXT REFERENCES task_states (name)
                          ON UPDATE CASCADE ON DELETE SET NULL,
    ADD COLUMN waiting_on TEXT CHECK (length(waiting_on) <= 100),
    -- Free text so each project keeps its own phases; a numbered prefix
    -- ("07 測試與安全驗證") is what puts them in order.
    ADD COLUMN phase      TEXT CHECK (length(phase) <= 40),
    -- WBS number, e.g. AG-6.2.
    ADD COLUMN code       TEXT CHECK (length(code) <= 20);
