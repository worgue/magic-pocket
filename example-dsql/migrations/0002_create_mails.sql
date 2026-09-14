-- schema.sql の mails テーブルを DSQL 方言で作る (0001 と同じ制約)。

CREATE TABLE mails (
    receipt_id text NOT NULL,
    subject text NOT NULL,
    sender text NOT NULL,
    recipients text NOT NULL,
    raw_key text NOT NULL,
    received_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (receipt_id)
);

CREATE INDEX ASYNC mails_received_at_idx ON mails (received_at);
