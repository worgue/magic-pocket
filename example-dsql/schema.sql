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
-- 【重要】この example が prune もページングも持たない理由:
-- DSQL は 1 クエリで取得できる行数が 3,000 件に制限される。1 日 1 行なので
-- 3,000 日 = 約 8.2 年はページングなしの素の SELECT で全件返せる。
-- それを超えたら一覧 API にページング (または期間絞り込み) が必要になる。
-- 「上限に当たるまでは単純に書く」という判断を意図的に残している。
CREATE TABLE messages (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    body text NOT NULL,
    source text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id)
);

CREATE INDEX messages_created_at_idx ON messages (created_at);
