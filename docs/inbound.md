# メール受信（inbound）

`[inbound.<名前>]` でSESの受信口を宣言できます。0.35.0で追加した機能です。
`inbox` や `mail` は利用者が付ける名前で、AWSの既存リソース名ではありません。
送信専用の `[ses]` とは独立しています。

## 設定例

```toml
[inbound.inbox]
domain = "receive.example.com"
recipients = ["test@receive.example.com"]
handler = "mail.worker"
retention_days = 365
# 受信処理の障害通知先。受信メールの転送先ではありません。
delivery_alert = { email = "ops@example.com" }

[container.mail]
dockerfile_path = "mail/Dockerfile"
permissions_boundary = "arn:aws:iam::123456789012:policy/lambda-boundary"

[container.mail.handlers.worker]
command = "app.mail.worker"
timeout = 120
sqs = { dead_letter_alert = { email = "ops@example.com" } }

[container.mail.handlers.importer]
command = "app.mail.import_job"
timeout = 120
```

`handler = "mail.worker"` は既存のCloudFront・schedulerと同じ参照形式で、
`container.mail.handlers.worker` を指します。同じcontainerのLambdaは実行roleを共有します。
メールへのアクセスを分離したい場合は専用containerにします。
boundaryは既存の `POCKET_PERMISSIONS_BOUNDARY_ARN` または設定ファイルを使い、
受信containerで未設定の場合は `FORGE_PERMISSIONS_BOUNDARY_ARN` を使います。

`retention_days` は必須で14日以上です。原本・受信情報・取り込みデータ・添付用prefixに
同じ保持期間を設定します。省略時のprefixは `raw/`、`metadata/`、`imports/`、`attachments/`。
`raw_prefix`、`metadata_prefix`、`import_prefix` は変更できますが、相互に重複できません。
初版は小文字の完全な受信アドレスを指定し、catch-allは扱いません。

stage名に特別な意味はありません。通常のstage上書きで受信先を分けます。
同じaccount/regionの複数stageで同じ宛先を受信する設定は競合として拒否します。

```toml
[dev.inbound.inbox]
domain = "receive-dev.example.com"
recipients = ["test@receive-dev.example.com"]
```

## 初期設定とdeploy

```bash
pocket resource inbound --stage dev --name inbox init
pocket deploy --stage dev
pocket resource inbound --stage dev --name inbox status
```

1. SES受信対応リージョンを選び、`init` のTXTとMXレコードをDNS管理者が登録します。
   `init` は既存active rule setをそのまま使います。存在しない場合だけ確認を挟み、
   共有の `pocket-inbound` を作成・有効化します。初期化はaccount/regionで同時実行しないでください。
   同名の既存セットは自動で取り込みません。
2. ドメインのSES検証が完了してからdeployします。`rule_set` と `after_rule` は不要です。
   active setの末尾に自分のルールを追加し、選択したセットをスタック出力に保存します。
   `rule_set` を明示する場合はactive setとの一致を必須とします。`after_rule` は設定項目にありません。
3. deploy後、通知先へ届くSNS購読確認メールのリンクを開きます。
   `pocket status` とdeploy末尾で未確認の購読を表示します。

既存の宛先重複・domain指定・catch-all、Stop/Bounce/Lambdaアクションは保守的に競合とします。
別宛先向けの停止処理も複数宛先メール全体へ影響するためです。既存ルールの並べ替えは行いません。
active set変更や管理中ルールの消失を検知した場合はdeployを停止します。
通常deployに共有セットの有効化・切替権限は含めません。

SES・S3・SNS・SQS・Lambdaは同じaccount/regionに配置します。
[SES受信対応リージョン一覧](https://docs.aws.amazon.com/general/latest/gr/ses.html#ses_region)を
確認してください。受信ドメインのMX変更は既存の配送にも影響するため、専用サブドメインが扱いやすい構成です。

## workerを書く

SESのS3ActionがMIME原本を非公開S3に保存し、そのSNS通知をSQSへ配送します。
`pocket.inbound.handler` は原本のVersionId・SHA256と完全なSNS/SES通知を
`metadata/<受信ID>.json` に保存してから、利用側の処理を呼びます。
Djangoや追加の実行時依存は不要です。SESの初期設定通知は保存し、業務処理と原本の欠落照合から除外します。

```python
from pocket.inbound import ReceivedMail, Receiver, handler


def process(mail: ReceivedMail) -> None:
    # 業務処理はmail.idを一意キーにして冪等化する。
    verdict = mail.metadata["ses"]["receipt"].get("virusVerdict", {}).get("status")
    if verdict != "PASS":
        return  # 原本と受信情報は保存済み。必要に応じて隔離状態をDBに記録する。
    message = mail.parse()
    # messageを解析してDBへ保存する、など。


worker = handler("inbox", process)


def import_job(event, context):
    mail = Receiver(event["inbound"]).load_import(event["manifest_key"])
    process(mail)
```

`mail.metadata["ses"]["receipt"]["recipients"]` はルールに一致した実際の受信宛先です。
MIMEのToヘッダーや `mail.destination` と区別して保存します。
`scan_enabled = true`（既定）は迷惑メール・ウイルス判定を付けますが、自動隔離はしません。
`tls_policy = "Require"` が既定です。必要な場合だけ `"Optional"` に変更します。
[SES通知のフィールド](https://docs.aws.amazon.com/ses/latest/dg/receiving-email-notifications-contents.html)

保存は条件付き書き込みで冪等化しますが、アプリの副作用を一度だけ実行する保証はありません。
既知の入力・AWSエラーはpartial batch responseで失敗レコードだけを再試行します。
それ以外の利用側例外は伝播し、バッチ全体を再試行します。いずれも業務側で重複を防ぎます。
本文・添付ファイル・HTMLは信頼できない入力として扱ってください。

受信メールを別アドレスへ転送する場合は `process` に送信処理を実装します。
`delivery_alert.email` は障害通知専用です。送信には別途 `[ses]` と検証済み送信元が必要です。

## 通知と復旧

配送失敗と解析失敗を区別します。

| 対象 | 検知・保存先 |
|---|---|
| SESのS3/SNSアクション失敗 | `PublishFailure` / `PublishExpired` alarm |
| SNSからSQSへの配送失敗 | 専用配送DLQとalarm |
| SQSからworkerの処理失敗 | 既存workerの処理DLQと `dead_letter_alert` |
| workerの処理滞留 | 受信queueの最古メッセージが1時間を超えるとalarm |
| 原本だけ存在する状態 | `reconcile` で対応する受信情報の欠落を検出 |

queueと両DLQの保持は14日です。原本・受信情報のバケットは非公開、versioning・SSE-S3・HTTPS必須。
workerには原本・受信情報の削除を明示的に拒否するIAMを設定します。
原本の存在確認のため、専用バケットのListBucketも許可します。

`delivery_alert` と既存の `dead_letter_alert` は同じ宣言型です。
emailの代わりに `{ eventbridge = true }` を指定すると、email購読なしでalarmを作ります。
明示的な無監視は `{ enabled = false }` で、stage上書き前の通知先が残っていても無効化を優先します。
有効時はemail・eventbridgeの一方を指定します。
受信スタックの出力にalarm ARNがあります。workerのalarm名は `<queue名>-dead-letter-alert` です。
EventBridgeの `source = aws.cloudwatch`、`detail-type = CloudWatch Alarm State Change` と
対象ARNで絞り、通知handler・retry・通知用DLQを利用側で接続します。
外部通知先への着信はpocketでは検証しません。
[CloudWatchのEventBridgeイベント](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/cloudwatch-and-eventbridge.html)

```bash
pocket resource inbound --stage dev --name inbox reconcile
```

SNSへの通知自体の失敗は配送DLQに入りません。SESは失敗したルールの実行を最大36時間再試行します。
原本だけ残った場合は不足を報告し、MIMEから受信情報を捏造しません。
通知遅延中も一時的に欠落として見えるため、DLQや配送状態と合わせて確認します。
原因を修正した後は `redrive --queue delivery` または `redrive --queue worker` で再投入できます。
共通オプション `--stage` と `--name` は他のコマンドと同じです。SNS envelope以外の形式は
推測で変換せず、DLQに残して中止します。再投入成功後だけ元のメッセージを削除します。
[SES受信メトリクス](https://docs.aws.amazon.com/ses/latest/dg/receiving-email-metrics.html)

## コピーと明示的な取り込み

```bash
pocket resource inbound --stage live --name inbox copy \
  --receipt-id <64桁の受信ID> --to-stage dev --to-name inbox
pocket resource inbound --stage dev --name inbox import \
  --manifest-key imports/<受信ID>/manifest.json --handler mail.importer
```

コピーは指定の1件だけを対象に、原本の保存済みVersionIdとhashを検証します。
`raw.eml` と `metadata.json` をコピーし、`manifest.json` を最後に保存します。
S3への書き込みでは解析は起動しません。`import` が同じcontainerの別handlerを同期実行します。
取り込み先では元のSNS topicや原本バケットを参照せず、コピーしたペアを使います。
メール原本だけのデータはこの経路で取り込みできません。

## 更新・撤去

imageだけの変更は受信を止めず、`pocket build` → `pocket promote` も同じ経路で対応します。
workerを先に作成し、その後で専用受信スタックを作ります。
受信ルールはバケット・ポリシー・subscription・alarmが揃ってから有効化します。

宛先を変更する前は `enabled = false` でdeployし、滞留を処理してください。
bucket・prefix・handlerの変更は新しいinbound名への移行として行い、旧データの参照先を保持します。
通常の設定更新で古い原本を見失う変更は拒否します。

撤去は `enabled = false` でdeploy後、36時間以上の配送猶予を置き、queue・両DLQを空にしてから
`pocket resource inbound --stage dev --name inbox destroy` を実行します。
SNSの未配送分や原本と受信情報の照合も確認してください。通常の `pocket destroy` でも
受信スタックを先に撤去します。宣言を消す前にこの手順を実行してください。

共有rule set・SES identity・原本バケット・受信queue・両DLQは保持します。
保持されたqueueやバケットは同名の再作成と衝突するので、再利用時は別のinbound名・worker名を使います。
Retainは無期限保存の指定ではなく、S3 lifecycleとSQSの保持期限は引き続き適用されます。

## 実環境での導入確認

ユニットテストとテンプレート検証に加え、利用環境で次を確認してください。
通常メール・複数宛先・添付付きメールの受信、原本と受信情報の対応、同じ通知の再送、
worker失敗時の処理DLQ、配送失敗時の配送DLQ、alarm通知の実着信、コピー後の明示取り込み。
DNS変更・SNS購読確認・実メールのE2E検証は、ライブラリの実装検証とは別に必要です。
