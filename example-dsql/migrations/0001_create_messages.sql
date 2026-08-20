-- schema.sql の messages テーブルを DSQL 方言で作る。
--
-- DSQL の制約:
-- - FOREIGN KEY は使えない (参照整合性はアプリ層で担保)
-- - SERIAL は使えない (主キーは uuid + gen_random_uuid())
-- - 二次インデックスは CREATE INDEX ASYNC で作る (作成は非同期。進行は sys.jobs)
-- - 1 トランザクションに DDL は 1 文まで (applier は 1 文ずつ autocommit で流す)

CREATE TABLE messages (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    body text NOT NULL,
    source text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE INDEX ASYNC messages_created_at_idx ON messages (created_at);
