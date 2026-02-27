use std::collections::HashMap;

use tracing::{info, warn};

use crate::config::PocketConfig;
use crate::error::{PocketError, Result};

/// AWS リソース情報を取得して環境変数にセットする
///
/// Python の runtime.py:set_envs_from_aws_resources() に相当
pub async fn set_envs_from_resources(config: &PocketConfig) -> Result<()> {
    // SAFETY: Lambda はシングルスレッドで起動時に1回のみ呼ばれる
    unsafe {
        std::env::set_var("POCKET_PROJECT_NAME", &config.project_name);
        std::env::set_var("POCKET_REGION", &config.region);
    }

    if config.handlers.is_empty() {
        unsafe {
            std::env::set_var("POCKET_HOSTS", "");
        }
        return Ok(());
    }

    let hosts_map = get_hosts(config).await?;
    let mut hosts = Vec::new();

    for (lambda_key, host) in &hosts_map {
        if let Some(host) = host {
            hosts.push(host.clone());
            let upper_key = lambda_key.to_uppercase();
            unsafe {
                std::env::set_var(format!("POCKET_{}_HOST", upper_key), host);
                std::env::set_var(
                    format!("POCKET_{}_ENDPOINT", upper_key),
                    format!("https://{}", host),
                );
            }
        }
    }

    unsafe {
        std::env::set_var("POCKET_HOSTS", hosts.join(""));
    }

    let queueurls_map = get_queueurls(config).await?;
    for (lambda_key, queueurl) in &queueurls_map {
        if let Some(url) = queueurl {
            let upper_key = lambda_key.to_uppercase();
            unsafe {
                std::env::set_var(format!("POCKET_{}_QUEUEURL", upper_key), url);
            }
        }
    }

    Ok(())
}

/// CFn stack output と handler config から全 handler の host を取得する
///
/// Python の runtime.py:_get_hosts() + _get_host() に相当
async fn get_hosts(config: &PocketConfig) -> Result<HashMap<String, Option<String>>> {
    let mut result = HashMap::new();

    // apigateway を持つ handler を収集
    let handlers_with_apigw: Vec<_> = config
        .handlers
        .iter()
        .filter(|(_, h)| h.apigateway.is_some())
        .collect();

    if handlers_with_apigw.is_empty() {
        return Ok(result);
    }

    // domain が明示されている handler はそのまま使う
    // CFn が必要な handler があれば stack output を取得
    let mut need_cfn = false;
    for (key, handler) in &handlers_with_apigw {
        if let Some(ag) = &handler.apigateway {
            if let Some(domain) = &ag.domain {
                result.insert(key.to_string(), Some(domain.clone()));
            } else {
                need_cfn = true;
            }
        }
    }

    if need_cfn {
        let stack_name = format!("{}-container", config.slug);
        let outputs = get_cfn_outputs(&config.region, &stack_name).await;

        for (key, handler) in &handlers_with_apigw {
            if result.contains_key(*key) {
                continue; // domain 指定済み
            }
            if handler.apigateway.is_some() {
                let output_key = format!("{}ApiEndpoint", capitalize(key));
                let host = outputs.as_ref().ok().and_then(|outs| {
                    outs.get(&output_key).map(|endpoint| {
                        // "https://xxx.execute-api.region.amazonaws.com" から "https://" を除去
                        endpoint
                            .strip_prefix("https://")
                            .unwrap_or(endpoint)
                            .to_string()
                    })
                });
                result.insert(key.to_string(), host);
            }
        }
    }

    Ok(result)
}

/// SQS get_queue_url で queue URL を取得する
///
/// Python の runtime.py:_get_queueurls() に相当
async fn get_queueurls(config: &PocketConfig) -> Result<HashMap<String, Option<String>>> {
    let mut result = HashMap::new();

    let handlers_with_sqs: Vec<_> = config
        .handlers
        .iter()
        .filter(|(_, h)| h.sqs.is_some())
        .collect();

    if handlers_with_sqs.is_empty() {
        return Ok(result);
    }

    let sdk_config = aws_config::defaults(aws_config::BehaviorVersion::latest())
        .region(aws_config::Region::new(config.region.clone()))
        .load()
        .await;
    let sqs_client = aws_sdk_sqs::Client::new(&sdk_config);

    for (key, handler) in &handlers_with_sqs {
        if let Some(sqs) = &handler.sqs {
            match sqs_client
                .get_queue_url()
                .queue_name(&sqs.name)
                .send()
                .await
            {
                Ok(resp) => {
                    result.insert(key.to_string(), resp.queue_url().map(|s| s.to_string()));
                }
                Err(e) => {
                    warn!("Failed to get queue URL for {}: {}", sqs.name, e);
                    result.insert(key.to_string(), None);
                }
            }
        }
    }

    Ok(result)
}

/// CFn stack の Outputs を HashMap<OutputKey, OutputValue> として取得する
async fn get_cfn_outputs(
    region: &str,
    stack_name: &str,
) -> Result<HashMap<String, String>> {
    let sdk_config = aws_config::defaults(aws_config::BehaviorVersion::latest())
        .region(aws_config::Region::new(region.to_string()))
        .load()
        .await;
    let cfn_client = aws_sdk_cloudformation::Client::new(&sdk_config);

    let resp = cfn_client
        .describe_stacks()
        .stack_name(stack_name)
        .send()
        .await
        .map_err(|e| PocketError::CloudFormation(e.to_string()))?;

    let mut outputs = HashMap::new();
    if let Some(stack) = resp.stacks().first() {
        for output in stack.outputs() {
            if let (Some(key), Some(value)) = (output.output_key(), output.output_value()) {
                outputs.insert(key.to_string(), value.to_string());
            }
        }
    }

    info!("CFn stack {} outputs: {:?}", stack_name, outputs.keys());
    Ok(outputs)
}

fn capitalize(s: &str) -> String {
    let mut chars = s.chars();
    match chars.next() {
        None => String::new(),
        Some(first) => {
            let upper: String = first.to_uppercase().collect();
            upper + chars.as_str()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_capitalize() {
        assert_eq!(capitalize("wsgi"), "Wsgi");
        assert_eq!(capitalize("worker"), "Worker");
        assert_eq!(capitalize(""), "");
        assert_eq!(capitalize("a"), "A");
    }
}
