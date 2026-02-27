use std::io;

#[derive(Debug, thiserror::Error)]
pub enum PocketError {
    #[error("pocket.toml not found (searched from CWD upward)")]
    TomlNotFound,

    #[error("failed to parse pocket.toml: {0}")]
    TomlParse(#[from] toml::de::Error),

    #[error("stage '{0}' not found in pocket.toml stages")]
    StageNotFound(String),

    #[error("SecretsManager error: {0}")]
    SecretsManager(String),

    #[error("SSM error: {0}")]
    Ssm(String),

    #[error("CloudFormation error: {0}")]
    CloudFormation(String),

    #[error("SQS error: {0}")]
    Sqs(String),

    #[error("unsupported secret type: {0}")]
    UnsupportedSecretType(String),

    #[error("JSON parse error: {0}")]
    JsonParse(#[from] serde_json::Error),

    #[error("IO error: {0}")]
    Io(#[from] io::Error),

    #[error("config error: {0}")]
    Config(String),
}

pub type Result<T> = std::result::Result<T, PocketError>;
