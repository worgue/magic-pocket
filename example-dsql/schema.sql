-- スキーマの信頼の源。素の PostgreSQL 方言で書き、
-- psqldef export の正規形で管理する。変更手順: このファイルを編集 →
-- `just schema-diff <name>` で migrations/ に DSQL 方言の SQL を生成 → レビュー → apply。
--
-- DSQL の制約:
-- - FOREIGN KEY は書かない (DSQL 非対応。参照整合性はアプリ層 + テストで担保)
-- - SERIAL は書かない (主キーは uuid DEFAULT gen_random_uuid() 等)
-- - インデックスはここでは素の CREATE INDEX で書く (diff が ASYNC へ機械変換する)

-- 日次スケジューラ (SQS 経由) が 1 日 1 行だけ追加するテーブル。削除はしない。
--
-- この example が prune もページングも持たない理由:
-- 1 日 1 行しか増えないため、数千行に達するまで年単位かかる。デモとして
-- 単純さを優先し、一覧 API はページングなしの素の SELECT にしてある。
-- 無制限に伸びる設計ではあるので、実運用ではページングか期間絞り込みを入れる。
--
-- なお DSQL の 3,000 行制限は **1 トランザクションで変更できる行数** (DML:
-- INSERT / UPDATE / DELETE) の上限であって、SELECT の取得行数の制限ではない。
-- この example は 1 トランザクション 1 行の INSERT だけなので抵触しない。
-- ただし将来まとめて削除する場合は 3,000 行以下にチャンク分割する必要がある。
CREATE TABLE messages (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    body text NOT NULL,
    source text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE INDEX messages_created_at_idx ON messages (created_at);
