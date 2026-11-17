//! AWS SDK まわりの小さな共通処理 (error の整形・region の差し替え)。

use anyhow::anyhow;
use aws_config::{Region, SdkConfig};
use aws_sdk_s3::error::{ProvideErrorMetadata, SdkError};

/// SDK の error を「操作名: code: message」の形にする
pub fn sdk_error<E, R>(operation: &str, error: &SdkError<E, R>) -> anyhow::Error
where
    E: ProvideErrorMetadata + std::fmt::Debug,
    R: std::fmt::Debug,
{
    match (error.code(), error.message()) {
        (Some(code), Some(message)) => anyhow!("{operation}: {code}: {message}"),
        _ => anyhow!("{operation}: {error:?}"),
    }
}

pub fn has_code<E: ProvideErrorMetadata, R>(error: &SdkError<E, R>, codes: &[&str]) -> bool {
    error.code().is_some_and(|code| codes.contains(&code))
}

/// 同じ認証情報で region だけ変えた設定
pub fn regional_config(sdk: &SdkConfig, region: &str) -> SdkConfig {
    sdk.to_builder()
        .region(Region::new(region.to_string()))
        .build()
}
