//! `pocket-rs resource`: resource 単位の操作 (Python 版 `pocket resource …`)。
//!
//! 読み取り・表示のコマンド (status / yaml / context / endpoint / url / secrets list /
//! status-env / image / inbound status・reconcile)。判定と template の描画は
//! `pocket-rs status` と同じもの ([`crate::status::aws::AwsEvaluator`]) を使う。
//!
//! Python 版との違い:
//! - `--stage` を省略したら入力を求めずエラー (env `POCKET_DEPLOY_STAGE` は読む)。
//!   VPC だけは Python 版と同じく省略でき、共通の `[vpc]` を使う
//! - Context の構築で、削除予約中の pocket managed secret を復元しない (`status` と同じ)
//! - `cloudfront` / `cloudfront-keys` の status・yaml は、managed secret (origin verify の
//!   値・署名の公開鍵) を読んでから template を描く (`pocket status` と同じ判定)

mod container;
mod db;
mod edge;
mod inbound;
mod pyrepr;
mod vpc;
mod write;

use anyhow::{bail, Result};
use clap::{Args, Subcommand, ValueEnum};
use magic_pocket_settings::context::{ContainerContext, Context};

use crate::context::load_context_read_only;
use crate::status::aws::AwsEvaluator;
use crate::status::sink::Terminal;

#[derive(Subcommand)]
pub enum ResourceCommand {
    /// VPC (CloudFormation stack と EFS)
    Vpc {
        #[command(subcommand)]
        command: VpcCommand,
    },
    /// Lambda container (CloudFormation stack・ECR・secret)
    Container {
        #[command(subcommand)]
        command: ContainerCommand,
    },
    /// S3 bucket
    S3 {
        #[command(subcommand)]
        command: S3Command,
    },
    /// Aurora PostgreSQL (RDS)
    Rds {
        #[command(subcommand)]
        command: DbCommand,
    },
    /// Aurora DSQL
    Dsql {
        #[command(subcommand)]
        command: DbCommand,
    },
    /// CloudFront distribution
    Cloudfront {
        #[command(subcommand)]
        command: CloudFrontCommand,
    },
    /// CloudFront の署名鍵 (public key・key group)
    CloudfrontKeys {
        #[command(subcommand)]
        command: EdgeCommand,
    },
    /// CloudFront の WAF (IP set・WebACL)
    CloudfrontWaf {
        #[command(subcommand)]
        command: EdgeCommand,
    },
    /// deploy 済みコンテナイメージの参照情報
    Image {
        #[command(subcommand)]
        command: ImageCommand,
    },
    /// SES 受信口
    Inbound {
        #[command(flatten)]
        target: inbound::InboundTarget,
        #[command(subcommand)]
        command: InboundCommand,
    },
}

/// stage (必須。`POCKET_DEPLOY_STAGE` を読む)
#[derive(Args, Clone)]
pub struct Stage {
    /// stage 名
    #[arg(long, env = "POCKET_DEPLOY_STAGE")]
    pub stage: String,
}

/// stage と container 名
#[derive(Args, Clone)]
pub struct ContainerTarget {
    #[command(flatten)]
    pub stage: Stage,
    /// 対象 container 名 (1 つだけなら省略可)
    #[arg(long = "container")]
    pub container: Option<String>,
}

/// stage と cloudfront 名
#[derive(Args, Clone)]
pub struct EdgeTarget {
    #[command(flatten)]
    pub stage: Stage,
    /// 対象の cloudfront 名 (省略時は全部)
    #[arg(long)]
    pub name: Option<String>,
}

#[derive(Args, Clone)]
pub struct VpcTarget {
    /// 対象ステージ。省略時は共通の [vpc] を使用
    #[arg(long, env = "POCKET_DEPLOY_STAGE")]
    pub stage: Option<String>,
}

#[derive(Subcommand)]
pub enum VpcCommand {
    /// 描画した template を表示する
    Yaml(VpcTarget),
    /// stack の状態を表示する
    Status(VpcTarget),
    /// stack を作る (完了は待たない)
    Create(VpcTarget),
    /// stack を更新する (template が変わっていれば)
    Update(VpcTarget),
    /// stack と EFS を削除する (確認あり)
    Destroy(VpcTarget),
}

#[derive(Subcommand)]
pub enum ContainerCommand {
    /// 描画した template を表示する
    Yaml(ContainerTarget),
    /// container の状態を表示する
    Status(ContainerTarget),
    /// wsgi handler の URL を表示する
    Url {
        #[command(flatten)]
        target: ContainerTarget,
        /// URL のこの path をブラウザで開く
        #[arg(long)]
        openpath: Option<String>,
    },
    /// secret の操作
    Secrets {
        #[command(subcommand)]
        command: SecretsCommand,
    },
    /// Lambda の現在 env と SSM/SM 上の宣言値の drift を表示する
    StatusEnv {
        #[command(flatten)]
        target: ContainerTarget,
        /// 特定 handler のみ対象 (省略時は全 handler)
        #[arg(long)]
        handler: Option<String>,
    },
    /// stack を作る (image の build はしない)
    Create(ContainerTarget),
    /// image の差し替え・stack の更新・DEPLOY_HASH の同期
    Update(ContainerTarget),
    /// Container 関連リソース (全 container) を削除する
    ///
    /// トップレベル `pocket-rs destroy` と同じ実装を使う (共有 ECR の削除ガード /
    /// stack 削除完了待ち / CodeBuild / log group 掃除を含む)
    Destroy {
        #[command(flatten)]
        stage: Stage,
        /// pocket managed secret も削除する
        #[arg(long)]
        with_secrets: bool,
        /// 確認プロンプトをスキップ
        #[arg(long, short = 'y')]
        yes: bool,
    },
    /// SSM/Secrets Manager の最新値で Lambda env を即時更新する (CFn を介さない)
    ///
    /// deploy 時の CFn snapshot を base に、secrets (managed + user) の最新値で上書きして
    /// Lambda に反映する。warm container 内の古い環境変数もリセットされる。
    /// 次の deploy で CFn の値に戻る
    ReloadEnv {
        #[command(flatten)]
        target: ContainerTarget,
        /// 特定 handler のみ対象 (省略時は全 handler)
        #[arg(long)]
        handler: Option<String>,
    },
}

#[derive(Subcommand)]
pub enum SecretsCommand {
    /// 宣言された secret と、pocket managed secret の作成状況を一覧する
    List {
        #[command(flatten)]
        stage: Stage,
        /// 値も表示する (マスクしない)
        #[arg(long)]
        show_values: bool,
    },
    /// pocket managed secret を作る (既にあるものは作り直さない)
    CreatePocketManaged(Stage),
    /// pocket managed secret を削除する (確認あり)
    DeletePocketManaged(Stage),
}

#[derive(Subcommand)]
pub enum S3Command {
    /// S3 の Context を表示する
    Context(Stage),
    /// bucket の有無を表示する
    Status(Stage),
    /// S3 バケットを作成、または既存バケットの設定 (PAB / CORS / versioning /
    /// lifecycle) を冪等に reconcile する
    Create(Stage),
    /// bucket を中身ごと削除する (確認あり)
    Destroy(Stage),
}

#[derive(Clone, Copy, ValueEnum)]
pub enum Format {
    Text,
    Json,
}

#[derive(Subcommand)]
pub enum DbCommand {
    /// クラスター状態表示
    Status(Stage),
    /// 接続情報表示
    Endpoint {
        #[command(flatten)]
        stage: Stage,
        /// 出力形式。text: 人間向け (stderr・色付き) / json: 機械可読 (stdout)
        #[arg(long = "format", value_enum, default_value = "text")]
        format: Format,
    },
    /// 確認付き削除
    Destroy(Stage),
}

#[derive(Subcommand)]
pub enum CloudFrontCommand {
    /// 描画した template を表示する
    Yaml(EdgeTarget),
    /// CloudFront の Context を JSON で表示する (secret はマスク)
    Context(EdgeTarget),
    /// stack の状態を表示する
    Status(EdgeTarget),
    /// stack を作る (create と update は同じ手順。完了を待って bucket policy と KVS を書く)
    Create(EdgeTarget),
    /// stack を更新する
    Update(EdgeTarget),
    /// distribution を削除する (bucket は残す)
    Destroy {
        #[command(flatten)]
        target: EdgeTarget,
        /// 確認プロンプトをスキップ
        #[arg(long, short = 'y')]
        yes: bool,
    },
    /// build / upload_dir を持つ route を build して S3 へ上げる
    Upload {
        #[command(flatten)]
        target: EdgeTarget,
        /// build を実行しない
        #[arg(long)]
        skip_build: bool,
    },
    /// ローカルの成果物に無い S3 key (旧版) を削除する
    ///
    /// delete_stale = false (content_hash の既定) で残した旧版の掃除用。ビルドは
    /// 実行しないので、upload_dir を最新のビルドにしてから実行する
    Prune {
        #[command(flatten)]
        target: EdgeTarget,
        /// 対象 route の path_pattern (省略時は build / upload_dir を持つ全 route)
        #[arg(long = "route")]
        path_pattern: Option<String>,
        /// 削除せず一覧だけ表示
        #[arg(long)]
        dry_run: bool,
        /// 確認プロンプトをスキップ
        #[arg(long, short = 'y')]
        yes: bool,
    },
}

#[derive(Subcommand)]
pub enum EdgeCommand {
    /// 描画した template を表示する
    Yaml(EdgeTarget),
    /// stack の状態を表示する
    Status(EdgeTarget),
    /// stack を削除する
    Destroy {
        #[command(flatten)]
        target: EdgeTarget,
        /// 確認プロンプトをスキップ
        #[arg(long, short = 'y')]
        yes: bool,
    },
}

#[derive(Subcommand)]
pub enum ImageCommand {
    /// ECR repository 名を stdout に出力する (ecr_name 上書きを含め toml 準拠)
    Repo(ContainerTarget),
    /// deploy 済み実イメージの URI ({repo_uri}@{digest}) を stdout に出力する
    ///
    /// `:{stage}` タグが現在指している image を digest で固定した URI を返すため、
    /// 以後の deploy でタグが動いても参照が壊れない
    Uri(ContainerTarget),
}

#[derive(Subcommand)]
pub enum InboundCommand {
    /// スタック・active set・ドメインの検証状態・監視先を表示する
    Status,
    /// active setと既存bindingを照会しテンプレートを表示する
    Yaml,
    /// 原本に対応する受信情報の欠落を検出する（復元・推測はしない）
    Reconcile,
    /// 無効化・配送猶予・滞留解消後に自分のルールとスタックを削除する
    ///
    /// 同じdomainを使う受信口が他に残っていなければ、domainのstack
    /// (SES identity / DKIM / MX) も削除する
    Destroy {
        /// 確認プロンプトをスキップ
        #[arg(long, short = 'y')]
        yes: bool,
    },
}

pub async fn run(command: ResourceCommand) -> Result<()> {
    let mut sink = Terminal::new();
    match command {
        ResourceCommand::Vpc { command } => vpc::run(command, &mut sink).await,
        ResourceCommand::Container { command } => container::run(command, &mut sink).await,
        ResourceCommand::S3 { command } => db::run_s3(command, &mut sink).await,
        ResourceCommand::Rds { command } => db::run_rds(command, &mut sink).await,
        ResourceCommand::Dsql { command } => db::run_dsql(command, &mut sink).await,
        ResourceCommand::Cloudfront { command } => edge::run_cloudfront(command, &mut sink).await,
        ResourceCommand::CloudfrontKeys { command } => edge::run_keys(command, &mut sink).await,
        ResourceCommand::CloudfrontWaf { command } => edge::run_waf(command, &mut sink).await,
        ResourceCommand::Image { command } => container::run_image(command).await,
        ResourceCommand::Inbound { target, command } => {
            inbound::run(&target, command, &mut sink).await
        }
    }
}

/// stage の Context (AWS に何も書かない) と、AWS の設定
async fn load(stage: &str) -> Result<(Context, aws_config::SdkConfig)> {
    let (context, _toml_dir) = load_context_read_only(stage)?;
    let sdk = crate::aws_util::load_sdk().await;
    Ok((context, sdk))
}

fn evaluator<'a>(context: &'a Context, sdk: &aws_config::SdkConfig) -> AwsEvaluator<'a> {
    AwsEvaluator::new(context, sdk.clone())
}

/// 操作対象の container 名 (Python の `pocket.runtime.resolve_container_name`)。
///
/// 優先順: 明示の引数 > `POCKET_CONTAINER` > container が 1 つだけならそれ
pub fn resolve_container<'c>(
    context: &'c Context,
    name: Option<&str>,
) -> Result<&'c ContainerContext> {
    let env = std::env::var("POCKET_CONTAINER").ok();
    let mut defined: Vec<&String> = context.container.keys().collect();
    defined.sort();
    let defined = defined
        .iter()
        .map(|s| s.as_str())
        .collect::<Vec<_>>()
        .join(", ");
    match name.or(env.as_deref()).filter(|n| !n.is_empty()) {
        Some(name) => match context.container.get(name) {
            Some(c) => Ok(c),
            None => bail!("container '{name}' は設定にありません。定義済み: {defined}"),
        },
        None if context.container.len() == 1 => {
            Ok(context.container.values().next().expect("1 つある"))
        }
        None => bail!(
            "複数の container が定義されています ({defined})。POCKET_CONTAINER 環境変数か \
             --container で対象を指定してください。"
        ),
    }
}
