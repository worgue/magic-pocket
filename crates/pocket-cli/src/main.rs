//! `pocket-rs`: magic-pocket の Rust 版 CLI。
//!
//! object_hash route の publish / adopt / purge / status / hash generate と、
//! 設定・Context の表示 (`settings` / `context`)、版の表示 (`version`)、image の build
//! (`build`) を行う。
//! サブコマンド・引数・出力は旧 Python 版 `pocket static ...` (削除済み) を引き継いでいる。
//! 静的系の実装はこちらだけで、Python 版の `pocket static` は移行先の案内だけを出す。

mod aws_util;
mod backup_cleanup;
mod context;
mod deploy;
mod django;
mod ec2;
mod image_build;
mod mapfile;
mod permissions;
mod publisher;
mod release;
mod resource;
mod sources;
mod stack;
mod status;
mod waf;

use std::io::{self, BufRead, Write};
use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{bail, Result};
use clap::{Args, Parser, Subcommand};
use magic_pocket_rs::config::{find_toml_path, load_static_config_from_path};

use crate::publisher::{status_report, PublishOptions, StaticPublisher};

#[derive(Parser)]
#[command(name = "pocket-rs", version, about = "magic-pocket の Rust 版 CLI")]
struct Cli {
    #[command(subcommand)]
    command: Top,
}

#[derive(Subcommand)]
enum Top {
    /// 内容 hash の immutable key で配信する静的ファイル (object_hash) の操作
    Static {
        #[command(subcommand)]
        command: StaticCommand,
    },
    /// Settings を JSON で出力する (pocket.toml のみ、AWS 不要)。secret はマスク
    Settings {
        /// stage 名
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// Context を JSON で出力する (AWS API 呼び出しを伴う)。secret はマスク
    Context {
        /// stage 名
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
    },
    /// deploy が管理する resource の状態を一覧で出す (AWS に何も書かない)
    Status {
        /// stage 名
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        /// 各 resource の説明と Context の JSON (secret はマスク) も出す
        #[arg(long)]
        show_info: bool,
    },
    /// 版を 1 行で出力する (`--version` と違い、版の番号だけを出す)
    Version,
    /// 作業ツリーを build し、git commit hash を tag にして ECR へ push する (deploy はしない)
    ///
    /// tag は COMMIT_HASH 環境変数があればそれを、無ければ `git rev-parse HEAD` を使う。
    /// commit hash と image 内容の一致が前提のため、working tree が dirty ならエラーで止まる
    Build {
        /// stage 名
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        /// working tree が dirty でも build する (ローカル検証用)
        #[arg(long)]
        allow_dirty: bool,
    },
    /// stage を deploy する (image の build → リソースの作成・更新 → 静的ファイルの upload)
    Deploy {
        /// stage 名
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        /// deploy の後に、URL のこの path をブラウザで開く
        #[arg(long)]
        openpath: Option<String>,
        /// route の upload をしない
        #[arg(long)]
        skip_frontend: bool,
        /// 確認プロンプトを自動で承認する
        #[arg(long, short = 'y')]
        yes: bool,
    },
    /// build 済みの `:<commit-hash>` image へ stage を向けて deploy する (再 build しない)
    ///
    /// `pocket-rs build` で push した image に `:<stage>` の tag を移し、リソースと Lambda を
    /// 更新する
    Promote {
        /// stage 名
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        /// 昇格する image の git commit hash
        #[arg(long)]
        commit_hash: String,
        /// deploy の後に、URL のこの path をブラウザで開く
        #[arg(long)]
        openpath: Option<String>,
        /// route の upload をしない
        #[arg(long)]
        skip_frontend: bool,
        /// 確認プロンプトを自動で承認する
        #[arg(long, short = 'y')]
        yes: bool,
    },
    /// stage の全リソースを一括で削除する (削除対象を一覧で示し、確認してから消す)
    Destroy {
        /// stage 名
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        /// シークレットを削除せずに残す
        #[arg(long)]
        without_secrets: bool,
        /// state bucket も削除する
        #[arg(long)]
        with_state_bucket: bool,
        /// 確認プロンプトを自動で承認する (backup のデータは `-y` では削除しない)
        #[arg(long, short = 'y')]
        yes: bool,
    },
    /// AWS Backup のバックアップデータの操作
    Backup {
        #[command(subcommand)]
        command: backup_cleanup::BackupCommand,
    },
    /// 旧バージョンが残した account 共有リソースを削除する。
    ///
    /// 対象は 0.36 以前の backup vault (pocket-backup) とサービスロール
    /// (forge-pocket-backup-role)。AWS account を共有している全 stage・全 project を
    /// 0.37.0 以上で deploy し終えてから、account/region ごとに 1 回実行する
    /// (--stage は region の解決に使う)。旧リソースをまだ参照している plan /
    /// selection があれば中止する。IAM ロールは全 region 共通なので、複数 region で
    /// pocket を使っている場合は全 region の更新後に実行すること。
    CleanupDeprecated {
        /// stage 名
        #[arg(long, env = "POCKET_DEPLOY_STAGE")]
        stage: String,
        /// 旧 vault に残るバックアップデータ (recovery point) も削除する
        /// (account 内の他 project の分も含む)
        #[arg(long)]
        delete_recovery_points: bool,
        /// 対象の表示だけ行う
        #[arg(long)]
        dry_run: bool,
        /// 確認プロンプトをスキップ
        #[arg(long, short = 'y')]
        yes: bool,
    },
    /// resource 単位の操作 (状態・template・接続情報の表示など)
    Resource {
        #[command(subcommand)]
        command: resource::ResourceCommand,
    },
    /// Django の運用 (雛形の生成・management command の実行・DB のリセット)
    Django {
        #[command(subcommand)]
        command: django::DjangoCommand,
    },
    /// IAM 権限関連 (deploy に要る Action・pocket が使う role の定義と名前)
    Permissions {
        #[command(subcommand)]
        command: permissions::PermissionsCommand,
    },
    /// CloudFront の WAF の運用 (IP allowlist・rate limit)
    Waf {
        #[command(subcommand)]
        command: waf::WafCommand,
    },
    /// deploy の部品を 1 つずつ実行する (e2e 用の非表示コマンド)
    #[command(hide = true)]
    DeployParts {
        #[command(subcommand)]
        part: deploy::parts::DeployPart,
    },
}

#[derive(Args, Clone)]
struct Target {
    /// stage 名
    #[arg(long, env = "POCKET_DEPLOY_STAGE")]
    stage: String,
    /// 対象の cloudfront 名 (省略時は全部)
    #[arg(long)]
    name: Option<String>,
    /// 対象 route の ref か path_pattern (省略時は object_hash の全 route)
    #[arg(long = "route")]
    route_ref: Option<String>,
}

#[derive(Args, Clone)]
struct Jobs {
    /// hash / upload の並列数
    #[arg(long, default_value_t = 8)]
    jobs: usize,
}

#[derive(Subcommand)]
enum StaticCommand {
    /// sources を hash し、無い key だけ上げて release を切り替える
    ///
    /// 既定は commit 済みの map と照合する (prod 向け)。hash のずれ・map に無いファイル・
    /// map にあってローカルにも S3 にも無い path はエラーで止まる。途中で止まっても
    /// 再実行で続きから進む (上げ終わった key は skip)
    Publish {
        #[command(flatten)]
        target: Target,
        #[command(flatten)]
        jobs: Jobs,
        /// ローカルから map を作り直してから publish する (dev 向け。map は commit)
        #[arg(long)]
        hash_generate: bool,
        /// map が未 commit / 作業ツリーと差があっても publish する
        #[arg(long)]
        allow_dirty: bool,
        /// S3 に既にある key も HEAD で内容を照合する (既定は hash 付き名は skip)
        #[arg(long)]
        verify_existing: bool,
    },
    /// 既存の平置き key (同じ prefix の path そのまま) から hashed key を copy で作る
    ///
    /// 内容が一致する (ETag = MD5) 平置き key があれば CopyObject、無ければ通常の upload。
    /// deploy_hash / content_hash からの移行で、同じ内容を host から上げ直さない publish
    Adopt {
        #[command(flatten)]
        target: Target,
        #[command(flatten)]
        jobs: Jobs,
        /// publish と同じ
        #[arg(long)]
        hash_generate: bool,
        /// publish と同じ
        #[arg(long)]
        allow_dirty: bool,
    },
    /// map (path → 内容 hash) の操作
    Hash {
        #[command(subcommand)]
        command: HashCommand,
    },
    /// release が参照する object を全部消す (current と release 表は残す)
    ///
    /// 消してから次の publish までの間、その route の URL は 404 になる (配信を止める
    /// 操作)。purge → publish で同じ内容は同じ key に戻るので URL は変わらない
    Purge {
        #[command(flatten)]
        target: Target,
        /// 削除せず件数だけ表示
        #[arg(long)]
        dry_run: bool,
        /// 確認プロンプトをスキップ
        #[arg(long, short = 'y')]
        yes: bool,
    },
    /// current の release と、ローカル map の release の一致を表示する
    Status {
        #[command(flatten)]
        target: Target,
    },
}

#[derive(Subcommand)]
enum HashCommand {
    /// ローカルの sources を hash して map を書く (upload はしない)
    Generate {
        #[command(flatten)]
        target: Target,
        #[command(flatten)]
        jobs: Jobs,
    },
}

async fn s3_client(region: &str) -> aws_sdk_s3::Client {
    let config = aws_config::defaults(aws_config::BehaviorVersion::latest())
        .region(aws_config::Region::new(region.to_string()))
        .load()
        .await;
    // moto 等のカスタム endpoint (AWS_ENDPOINT_URL) では path-style で呼ぶ
    // (virtual-hosted style の `<bucket>.localhost` は解決できない環境がある)
    let custom_endpoint = ["AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL"]
        .iter()
        .any(|k| std::env::var(k).map(|v| !v.is_empty()).unwrap_or(false));
    let builder = aws_sdk_s3::config::Builder::from(&config).force_path_style(custom_endpoint);
    aws_sdk_s3::Client::from_conf(builder.build())
}

async fn get_publishers(target: &Target, jobs: usize) -> Result<Vec<Arc<StaticPublisher>>> {
    let toml_path = find_toml_path()?;
    let config = load_static_config_from_path(&toml_path, &target.stage)?;
    if config.cloudfront_names.is_empty() {
        bail!("cloudfront is not configured for this stage");
    }
    let Some(bucket) = config.s3_bucket_name.clone() else {
        bail!("s3 is not configured for this stage");
    };
    let base_dir: PathBuf = toml_path.parent().map(PathBuf::from).unwrap_or_default();
    if let Some(name) = &target.name {
        if !config.cloudfront_names.contains(name) {
            bail!("cloudfront '{name}' is not configured");
        }
    }
    let s3 = s3_client(&config.region).await;
    let mut publishers = Vec::new();
    for route in config.static_routes {
        if target.name.as_ref().is_some_and(|n| *n != route.cloudfront) {
            continue;
        }
        if let Some(r) = &target.route_ref {
            let url_pattern = format!("{}*", route.url_prefix);
            if route.route_ref.as_deref() != Some(r.as_str()) && *r != url_pattern {
                continue;
            }
        }
        publishers.push(Arc::new(StaticPublisher::new(
            route,
            bucket.clone(),
            base_dir.clone(),
            jobs,
            s3.clone(),
        )));
    }
    if publishers.is_empty() {
        bail!(
            "versioning = \"object_hash\" の route がありません{}",
            target
                .route_ref
                .as_ref()
                .map(|r| format!(" (route {r})"))
                .unwrap_or_default()
        );
    }
    Ok(publishers)
}

/// `pocket-rs settings`: Python の `pocket settings` と同じ JSON (マスク済み、indent 2)
fn print_settings(stage: &str) -> Result<()> {
    let toml_path = find_toml_path()?;
    let (settings, advisories) = magic_pocket_settings::Settings::load(&toml_path, stage)?;
    for advisory in advisories {
        eprintln!("{}", advisory.message);
    }
    let masked = magic_pocket_settings::mask_secret_values(&settings.to_json());
    println!("{}", magic_pocket_settings::pyjson::dumps_indent2(&masked));
    Ok(())
}

async fn run(cli: Cli) -> Result<()> {
    let command = match cli.command {
        Top::Static { command } => command,
        Top::Settings { stage } => return print_settings(&stage),
        Top::Context { stage } => return context::print_context(&stage).await,
        Top::Status { stage, show_info } => return status::run(&stage, show_info).await,
        Top::Build { stage, allow_dirty } => {
            return image_build::run_build(&stage, allow_dirty).await
        }
        Top::Deploy {
            stage,
            openpath,
            skip_frontend,
            yes,
        } => {
            return deploy::run::run(deploy::run::DeployArgs {
                stage,
                promote: None,
                openpath,
                skip_frontend,
                yes,
            })
            .await
        }
        Top::Promote {
            stage,
            commit_hash,
            openpath,
            skip_frontend,
            yes,
        } => {
            return deploy::run::run(deploy::run::DeployArgs {
                stage,
                promote: Some(commit_hash),
                openpath,
                skip_frontend,
                yes,
            })
            .await
        }
        Top::Destroy {
            stage,
            without_secrets,
            with_state_bucket,
            yes,
        } => {
            return deploy::destroy_run::run(deploy::destroy_run::DestroyArgs {
                stage,
                without_secrets,
                with_state_bucket,
                yes,
            })
            .await
        }
        Top::Resource { command } => return resource::run(command).await,
        Top::Waf { command } => return waf::run(command).await,
        Top::Permissions { command } => return permissions::run(command).await,
        Top::Django { command } => return django::run(command).await,
        Top::Backup { command } => {
            let mut sink = status::sink::Terminal::new();
            return backup_cleanup::run_backup(command, &mut sink).await;
        }
        Top::CleanupDeprecated {
            stage,
            delete_recovery_points,
            dry_run,
            yes,
        } => {
            let mut sink = status::sink::Terminal::new();
            return backup_cleanup::run_cleanup_deprecated(
                &stage,
                delete_recovery_points,
                dry_run,
                yes,
                &mut sink,
            )
            .await;
        }
        Top::DeployParts { part } => return deploy::parts::run(part).await,
        Top::Version => {
            println!("{}", env!("CARGO_PKG_VERSION"));
            return Ok(());
        }
    };
    match command {
        StaticCommand::Publish {
            target,
            jobs,
            hash_generate,
            allow_dirty,
            verify_existing,
        } => {
            for publisher in get_publishers(&target, jobs.jobs).await? {
                publisher
                    .publish(PublishOptions {
                        hash_generate,
                        allow_dirty,
                        adopt: false,
                        verify_existing,
                    })
                    .await?;
            }
        }
        StaticCommand::Adopt {
            target,
            jobs,
            hash_generate,
            allow_dirty,
        } => {
            for publisher in get_publishers(&target, jobs.jobs).await? {
                publisher
                    .publish(PublishOptions {
                        hash_generate,
                        allow_dirty,
                        adopt: true,
                        verify_existing: false,
                    })
                    .await?;
            }
        }
        StaticCommand::Hash {
            command: HashCommand::Generate { target, jobs },
        } => {
            for publisher in get_publishers(&target, jobs.jobs).await? {
                let mut files = publisher.enumerate_sources()?;
                publisher.hash_files(&mut files).await?;
                let static_map = publisher.build_map(&files)?;
                let path = publisher.write_map(&static_map)?;
                eprintln!(
                    "[{}] {} を書きました ({} 件, release {})",
                    publisher.route_label(),
                    path.display(),
                    files.len(),
                    static_map.release_id()
                );
            }
        }
        StaticCommand::Purge {
            target,
            dry_run,
            yes,
        } => {
            let publishers = get_publishers(&target, 8).await?;
            let mut targets: Vec<(Arc<StaticPublisher>, Vec<String>)> = Vec::new();
            for publisher in publishers {
                let keys = publisher.purge(true).await?;
                eprintln!("[{}] 削除対象 {} 件", publisher.route_label(), keys.len());
                if !keys.is_empty() {
                    targets.push((publisher, keys));
                }
            }
            let total: usize = targets.iter().map(|(_, k)| k.len()).sum();
            if total == 0 {
                eprintln!("削除対象はありません");
                return Ok(());
            }
            if dry_run {
                eprintln!("--dry-run のため削除しません ({total} 件)");
                return Ok(());
            }
            publisher::warn("消してから次の publish までの間、この route の URL は 404 になります");
            if !yes && !confirm(&format!("上記 {total} 件を削除しますか？"))? {
                bail!("Aborted!");
            }
            for (publisher, _) in &targets {
                let deleted = publisher.purge(false).await?;
                eprintln!(
                    "[{}] {} 件を削除しました",
                    publisher.route_label(),
                    deleted.len()
                );
            }
            eprintln!("{total} 件を削除しました");
        }
        StaticCommand::Status { target } => {
            for publisher in get_publishers(&target, 8).await? {
                let report = status_report(&publisher).await?;
                println!("{}", serde_json::to_string_pretty(&report)?);
                let label = publisher.route_label();
                if report["in_sync"].as_bool() == Some(true) {
                    eprintln!("[{label}] current は map と一致しています");
                } else if let Some(current) = report.get("current_release").and_then(|v| v.as_str())
                {
                    publisher::warn(&format!(
                        "[{label}] current ({current}) と map ({}) が違います",
                        report
                            .get("map_release")
                            .and_then(|v| v.as_str())
                            .unwrap_or("無し")
                    ));
                } else {
                    publisher::warn(&format!("[{label}] まだ publish されていません"));
                }
            }
        }
    }
    Ok(())
}

/// click.confirm 相当 (`[y/N]`。stdin が閉じていれば No)
fn confirm(prompt: &str) -> Result<bool> {
    print!("{prompt} [y/N]: ");
    io::stdout().flush()?;
    let mut line = String::new();
    io::stdin().lock().read_line(&mut line)?;
    Ok(matches!(
        line.trim().to_ascii_lowercase().as_str(),
        "y" | "yes"
    ))
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();
    if let Err(e) = run(cli).await {
        eprintln!("Error: {e:#}");
        std::process::exit(1);
    }
}
