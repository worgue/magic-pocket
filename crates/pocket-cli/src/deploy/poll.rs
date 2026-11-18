//! 完了待ち (Python 版 `resources/aws/poll.py` の `wait_until`)。

use std::future::Future;
use std::io::Write as _;

use anyhow::{bail, Result};

use crate::stack::ops::WaitOptions;

/// `poll` が `true` を返すまで待つ。回数が尽きたら `timeout_message` で止める
/// (黙って先へ進むと、待機が意味を持たないまま後続の処理が走る)。
///
/// 進行は 1 行の点で出す (`start_message.....`)。`poll` の中の error はそのまま返す
pub async fn wait_until<F, Fut>(
    options: WaitOptions,
    start_message: &str,
    timeout_message: &str,
    mut poll: F,
) -> Result<()>
where
    F: FnMut() -> Fut,
    Fut: Future<Output = Result<bool>>,
{
    for i in 0..options.attempts {
        if poll().await? {
            println!();
            return Ok(());
        }
        if i == 0 {
            print!("{start_message}");
        }
        print!(".");
        std::io::stdout().flush().ok();
        tokio::time::sleep(options.interval).await;
    }
    bail!("{timeout_message}")
}

#[cfg(test)]
pub mod testing {
    use std::time::Duration;

    use super::*;

    /// 待たずに `attempts` 回だけ見る
    pub fn fast(attempts: u64) -> WaitOptions {
        WaitOptions {
            attempts,
            interval: Duration::ZERO,
            timeout_secs: 0,
        }
    }

    pub fn run<T>(future: impl Future<Output = T>) -> T {
        tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .expect("runtime")
            .block_on(future)
    }
}

#[cfg(test)]
mod tests {
    use std::cell::Cell;

    use super::testing::{fast, run};
    use super::*;

    #[test]
    fn test_wait_until_polls_until_true() {
        let calls = Cell::new(0);
        run(wait_until(fast(5), "waiting", "timeout", || async {
            calls.set(calls.get() + 1);
            Ok(calls.get() == 3)
        }))
        .expect("3 回目で完了");
        assert_eq!(calls.get(), 3);
    }

    #[test]
    fn test_wait_until_times_out_and_propagates_errors() {
        let error = run(wait_until(fast(2), "waiting", "did not finish", || async {
            Ok(false)
        }))
        .expect_err("timeout");
        assert_eq!(error.to_string(), "did not finish");
        let error = run(wait_until(fast(2), "waiting", "timeout", || async {
            bail!("AccessDenied")
        }))
        .expect_err("error");
        assert_eq!(error.to_string(), "AccessDenied");
    }
}
