-- Review items the user looked at in /app and chose to keep, so the review
-- inbox stops showing them. The key embeds what raised the item (a stale
-- memory's timestamp, an overview's last update), so a new reason to look
-- again gets a new key and shows up again.

CREATE TABLE review_dismissals (
    kind          TEXT NOT NULL,
    key           TEXT NOT NULL,
    dismissed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, key)
);
