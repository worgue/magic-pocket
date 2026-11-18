//! AWS SDK まわりの小さな共通処理 (error の整形・region の差し替え)。

use anyhow::anyhow;
use aws_config::{Region, SdkConfig};
use aws_sdk_s3::error::{ProvideErrorMetadata, SdkError};

/// AWS の API が返した error (code が取れたもの)。
///
/// 表示は「操作名: code: message」。`anyhow::Error` に包んだ後も [`error_code`] で code を
/// 取り出せる (権限不足を警告に留める、といった分岐に使う)
#[derive(Debug)]
pub struct AwsError {
    pub operation: String,
    pub code: String,
    pub message: String,
}

impl std::fmt::Display for AwsError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}: {}", self.operation, self.code, self.message)
    }
}

impl std::error::Error for AwsError {}

/// [`sdk_error`] が作った error の code
pub fn error_code(error: &anyhow::Error) -> Option<&str> {
    error
        .downcast_ref::<AwsError>()
        .map(|aws| aws.code.as_str())
}

/// SDK の error を「操作名: code: message」の形にする
pub fn sdk_error<E, R>(operation: &str, error: &SdkError<E, R>) -> anyhow::Error
where
    E: ProvideErrorMetadata + std::fmt::Debug,
    R: std::fmt::Debug,
{
    match (error.code(), error.message()) {
        (Some(code), Some(message)) => anyhow::Error::new(AwsError {
            operation: operation.to_string(),
            code: code.to_string(),
            message: message.to_string(),
        }),
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
