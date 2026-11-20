//! `pocket-rs resource s3` / `rds` / `dsql` の読み取り (Python 版 `s3_cli.py` の context・
//! status、`rds_cli.py` / `dsql_cli.py` の status・endpoint)。

use anyhow::{bail, Result};
use magic_pocket_settings::context::{DsqlContext, RdsContext};
use magic_pocket_settings::{mask_secret_values, pyjson};
use serde_json::{json, Value as Json};

use super::{load, pyrepr, DbCommand, Format, S3Command};
use crate::aws_util::{regional_config, sdk_error};
use crate::status::dsql::cluster_status;
use crate::status::rds::ClusterState;
use crate::status::report::Evaluator;
use crate::status::resource::Resource;
use crate::status::sink::Sink;

pub async fn run_s3(command: S3Command, sink: &mut dyn Sink) -> Result<()> {
    match command {
        S3Command::Context(stage) => {
            let (context, _sdk) = load(&stage.stage).await?;
            let Some(s3) = context.s3.as_ref() else {
                bail!("s3 is not configured for this stage");
            };
            let dump = mask_secret_values(&serde_json::to_value(s3)?);
            sink.print(&pyrepr::pformat(&dump));
            Ok(())
        }
        S3Command::Create(stage) => super::write::s3_create(&stage.stage, sink).await,
        S3Command::Destroy(stage) => super::write::s3_destroy(&stage.stage, sink).await,
        S3Command::Status(stage) => {
            let (context, sdk) = load(&stage.stage).await?;
            let Some(s3) = context.s3.as_ref() else {
                bail!("s3 is not configured for this stage");
            };
            let client = aws_sdk_s3::Client::new(&regional_config(&sdk, &s3.region));
            match client.head_bucket().bucket(&s3.bucket_name).send().await {
                Ok(_) => sink.success("Storage found"),
                Err(e) if e.raw_response().is_some_and(|r| r.status().as_u16() == 404) => {
                    sink.warning("Storage not found")
                }
                // 404 以外の応答 (403 など) は、他の account が同じ名前を使っている見込み
                Err(e) if e.raw_response().is_some() => bail!(
                    "Bucket might be already used by other account. Try another bucket_prefix."
                ),
                Err(e) => return Err(sdk_error("s3 head_bucket", &e)),
            }
            Ok(())
        }
    }
}

pub async fn run_rds(command: DbCommand, sink: &mut dyn Sink) -> Result<()> {
    match command {
        DbCommand::Destroy(stage) => super::write::rds_destroy(&stage.stage, sink).await,
        DbCommand::Status(stage) => {
            let (context, sdk) = load(&stage.stage).await?;
            let rds = rds_context(context.rds.as_ref())?;
            let mut evaluator = super::evaluator(&context, &sdk);
            let cluster = evaluator.rds_cluster(rds).await?;
            let status = evaluator.status(&Resource::Rds(rds), sink).await?;
            print_rds_status(rds, &status.to_string(), cluster.as_ref(), sink);
            Ok(())
        }
        DbCommand::Endpoint { stage, format } => {
            let (context, sdk) = load(&stage.stage).await?;
            let rds = rds_context(context.rds.as_ref())?;
            let evaluator = super::evaluator(&context, &sdk);
            let cluster = evaluator.rds_cluster(rds).await?;
            print_rds_endpoint(rds, cluster.as_ref(), format, sink)
        }
    }
}

pub async fn run_dsql(command: DbCommand, sink: &mut dyn Sink) -> Result<()> {
    match command {
        DbCommand::Destroy(stage) => super::write::dsql_destroy(&stage.stage, sink).await,
        DbCommand::Status(stage) => {
            let (context, sdk) = load(&stage.stage).await?;
            let dsql = dsql_context(context.dsql.as_ref())?;
            let evaluator = super::evaluator(&context, &sdk);
            let cluster = evaluator.dsql_cluster(dsql).await?;
            let status = cluster_status(cluster.as_ref().map(|(i, s)| (i.as_str(), s.as_str())))?;
            sink.info(&format!("Tag Name: {}", dsql.tag_name));
            sink.info(&format!("Status: {status}"));
            if let Some((identifier, _)) = cluster.as_ref() {
                sink.info(&format!("Identifier: {identifier}"));
            }
            Ok(())
        }
        DbCommand::Endpoint { stage, format } => {
            let (context, sdk) = load(&stage.stage).await?;
            let dsql = dsql_context(context.dsql.as_ref())?;
            let evaluator = super::evaluator(&context, &sdk);
            let identifier = evaluator.dsql_cluster(dsql).await?.map(|(i, _)| i);
            print_dsql_endpoint(dsql, identifier.as_deref(), format, sink)
        }
    }
}

fn rds_context(rds: Option<&RdsContext>) -> Result<&RdsContext> {
    match rds {
        Some(rds) => Ok(rds),
        None => bail!("rds is not configured for this stage"),
    }
}

fn dsql_context(dsql: Option<&DsqlContext>) -> Result<&DsqlContext> {
    match dsql {
        Some(dsql) => Ok(dsql),
        None => bail!("dsql is not configured for this stage"),
    }
}

fn print_rds_status(
    rds: &RdsContext,
    status: &str,
    cluster: Option<&ClusterState>,
    sink: &mut dyn Sink,
) {
    sink.info(&format!("Cluster: {}", rds.cluster_identifier));
    sink.info(&format!("Status: {status}"));
    if let Some(cluster) = cluster {
        sink.info(&format!(
            "Engine: {}",
            cluster.engine_version.as_deref().unwrap_or_default()
        ));
    }
}

/// 見つからないとき、json はスクリプト向けに exit 1、text は警告だけ (exit 0)
fn not_found(format: Format, sink: &mut dyn Sink) -> Result<()> {
    match format {
        Format::Json => bail!("Cluster not found"),
        Format::Text => {
            sink.warning("Cluster not found");
            Ok(())
        }
    }
}

fn print_rds_endpoint(
    rds: &RdsContext,
    cluster: Option<&ClusterState>,
    format: Format,
    sink: &mut dyn Sink,
) -> Result<()> {
    let Some(cluster) = cluster else {
        return not_found(format, sink);
    };
    let endpoint = cluster.endpoint.clone().unwrap_or_default();
    // Python は `cluster.get("Port", "")` (無ければ空文字)
    let port = cluster.port.map_or(json!(""), Json::from);
    match format {
        Format::Json => sink.print(&pyjson::dumps(&json!({
            "endpoint": endpoint,
            "port": port,
            "database": rds.database_name,
            "username": rds.master_username,
        }))),
        Format::Text => {
            let port = match &port {
                Json::String(s) => s.clone(),
                other => other.to_string(),
            };
            sink.success(&format!("Endpoint: {endpoint}"));
            sink.success(&format!("Port: {port}"));
            sink.success(&format!("Database: {}", rds.database_name));
            sink.success(&format!("Username: {}", rds.master_username));
        }
    }
    Ok(())
}

fn print_dsql_endpoint(
    dsql: &DsqlContext,
    identifier: Option<&str>,
    format: Format,
    sink: &mut dyn Sink,
) -> Result<()> {
    let Some(identifier) = identifier else {
        return not_found(format, sink);
    };
    let endpoint = crate::deploy::dsql::endpoint(dsql, identifier);
    match format {
        Format::Json => sink.print(&pyjson::dumps(&json!({
            "endpoint": endpoint,
            "region": dsql.region,
            "port": 5432,
        }))),
        Format::Text => {
            sink.success(&format!("Endpoint: {endpoint}"));
            sink.success(&format!("Region: {}", dsql.region));
            sink.success("Port: 5432");
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::vector_context;
    use crate::status::sink::{Recorded, Stream, Style};

    fn texts(sink: &Recorded) -> Vec<(Stream, Style, &str)> {
        sink.0
            .iter()
            .map(|(s, y, t)| (*s, *y, t.as_str()))
            .collect()
    }

    #[test]
    fn test_rds_endpoint_json_and_text() {
        let ctx = vector_context("toml/rds.dev");
        let rds = ctx.rds.as_ref().unwrap();
        let cluster = ClusterState {
            endpoint: Some("db.example".to_string()),
            port: Some(5432),
            ..Default::default()
        };
        let mut sink = Recorded::default();
        print_rds_endpoint(rds, Some(&cluster), Format::Json, &mut sink).unwrap();
        assert_eq!(
            sink.0[0].2,
            format!(
                "{{\"endpoint\": \"db.example\", \"port\": 5432, \"database\": \"{}\", \"username\": \"{}\"}}",
                rds.database_name, rds.master_username
            )
        );
        let mut sink = Recorded::default();
        let no_port = ClusterState::default();
        print_rds_endpoint(rds, Some(&no_port), Format::Text, &mut sink).unwrap();
        assert_eq!(
            texts(&sink)[0],
            (Stream::Stderr, Style::Success, "Endpoint: ")
        );
        assert_eq!(texts(&sink)[1].2, "Port: ");
    }

    #[test]
    fn test_not_found_is_error_only_for_json() {
        let ctx = vector_context("toml/rds.dev");
        let rds = ctx.rds.as_ref().unwrap();
        let mut sink = Recorded::default();
        print_rds_endpoint(rds, None, Format::Text, &mut sink).unwrap();
        assert_eq!(
            texts(&sink),
            [(Stream::Stderr, Style::Warning, "Cluster not found")]
        );
        let error = print_rds_endpoint(rds, None, Format::Json, &mut sink).unwrap_err();
        assert_eq!(error.to_string(), "Cluster not found");
    }

    #[test]
    fn test_rds_status_lines() {
        let ctx = vector_context("toml/rds.dev");
        let rds = ctx.rds.as_ref().unwrap();
        let cluster = ClusterState {
            engine_version: Some("16.4".to_string()),
            ..Default::default()
        };
        let mut sink = Recorded::default();
        print_rds_status(rds, "COMPLETED", Some(&cluster), &mut sink);
        let lines: Vec<&str> = sink.0.iter().map(|(_, _, t)| t.as_str()).collect();
        assert_eq!(
            lines,
            [
                format!("Cluster: {}", rds.cluster_identifier).as_str(),
                "Status: COMPLETED",
                "Engine: 16.4"
            ]
        );
    }
}
