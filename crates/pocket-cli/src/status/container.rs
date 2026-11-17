//! Container (stack + Lambda + ECR image) の状態 (Python 版 `resources/container.py`)。

use anyhow::{bail, Result};

use crate::stack::ops::ResourceStatus;

/// `get_function` のうち判定に使う項目。function が無ければ両方 `None`
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct HandlerState {
    pub last_update_status: Option<String>,
    /// deploy 済みの image の digest (`CodeSha256`)
    pub code_sha256: Option<String>,
}

pub fn handler_status(handler: &HandlerState) -> Result<ResourceStatus> {
    Ok(match handler.last_update_status.as_deref() {
        None => ResourceStatus::NoExist,
        Some("InProgress") => ResourceStatus::Progress,
        Some("Failed") => ResourceStatus::Failed,
        Some("Successful") => ResourceStatus::Completed,
        Some(_) => bail!("unexpected status"),
    })
}

/// ECR の image digest (`sha256:<hex>`) の hex 部分
pub fn image_hash(digest: Option<&str>) -> Result<Option<String>> {
    let Some(digest) = digest else {
        return Ok(None);
    };
    match digest.split_once(':') {
        Some(("sha256", hash)) => Ok(Some(hash.to_string())),
        _ => bail!("unsupported algorithm"),
    }
}

/// handler と stack の状態から Container の途中経過を決める。
/// `None` は「stack が COMPLETED なので image の照合へ進む」
pub fn status_before_image(
    handlers: &[ResourceStatus],
    stack: ResourceStatus,
) -> Option<ResourceStatus> {
    use ResourceStatus::*;
    if handlers.contains(&Failed) || stack == Failed {
        return Some(Failed);
    }
    if handlers.contains(&Progress) || stack == Progress {
        return Some(Progress);
    }
    if matches!(stack, NoExist | RequireUpdate) {
        return Some(stack);
    }
    None
}

/// 全 handler が ECR の今の image で動いているか
pub fn image_status(handlers: &[HandlerState], image_hash: Option<&str>) -> ResourceStatus {
    if handlers
        .iter()
        .any(|h| h.code_sha256.as_deref() != image_hash)
    {
        return ResourceStatus::RequireUpdate;
    }
    ResourceStatus::Completed
}

#[cfg(test)]
mod tests {
    use super::*;
    use ResourceStatus::*;

    fn handler(status: Option<&str>, hash: Option<&str>) -> HandlerState {
        HandlerState {
            last_update_status: status.map(str::to_string),
            code_sha256: hash.map(str::to_string),
        }
    }

    #[test]
    fn test_handler_status_table() {
        for (status, want) in [
            (None, NoExist),
            (Some("InProgress"), Progress),
            (Some("Failed"), Failed),
            (Some("Successful"), Completed),
        ] {
            assert_eq!(handler_status(&handler(status, None)).unwrap(), want);
        }
        assert!(handler_status(&handler(Some("Other"), None)).is_err());
    }

    #[test]
    fn test_failed_and_progress_win_over_stack() {
        assert_eq!(
            status_before_image(&[Completed, Failed], Completed),
            Some(Failed)
        );
        assert_eq!(status_before_image(&[Progress], Failed), Some(Failed));
        assert_eq!(status_before_image(&[Completed], Progress), Some(Progress));
        assert_eq!(
            status_before_image(&[Progress], RequireUpdate),
            Some(Progress)
        );
        assert_eq!(status_before_image(&[NoExist], NoExist), Some(NoExist));
        assert_eq!(
            status_before_image(&[Completed], RequireUpdate),
            Some(RequireUpdate)
        );
        // stack が COMPLETED なら handler が未作成でも image の照合へ進む
        assert_eq!(status_before_image(&[NoExist], Completed), None);
    }

    #[test]
    fn test_image_status_compares_every_handler() {
        let same = [handler(Some("Successful"), Some("abc"))];
        assert_eq!(image_status(&same, Some("abc")), Completed);
        let mixed = [
            handler(Some("Successful"), Some("abc")),
            handler(Some("Successful"), Some("old")),
        ];
        assert_eq!(image_status(&mixed, Some("abc")), RequireUpdate);
        // function が無い handler は image が無いときだけ一致
        assert_eq!(
            image_status(&[handler(None, None)], Some("abc")),
            RequireUpdate
        );
        assert_eq!(image_status(&[handler(None, None)], None), Completed);
    }

    #[test]
    fn test_image_hash() {
        assert_eq!(image_hash(None).unwrap(), None);
        assert_eq!(
            image_hash(Some("sha256:abc")).unwrap().as_deref(),
            Some("abc")
        );
        assert!(image_hash(Some("sha512:abc")).is_err());
    }
}
