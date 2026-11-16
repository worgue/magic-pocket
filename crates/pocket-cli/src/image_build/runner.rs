//! 外部コマンド (git / docker / depot) の実行。
//!
//! テストで fake に差し替えられるよう [`Runner`] trait を挟む。実装は
//! [`SystemRunner`] (std::process) だけ。

use std::collections::BTreeMap;
use std::io::Write;
use std::process::{Command, Stdio};

use anyhow::{bail, Context as _, Result};

/// 実行するコマンド 1 本
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Cmd {
    pub program: String,
    pub args: Vec<String>,
    /// `Some` なら環境変数をこれだけにする (親の環境は引き継がない)
    pub env: Option<BTreeMap<String, String>>,
    /// 標準入力に渡す内容 (`docker login --password-stdin` 用)
    pub stdin: Option<String>,
}

impl Cmd {
    pub fn new<I, S>(program: &str, args: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        Self {
            program: program.to_string(),
            args: args.into_iter().map(Into::into).collect(),
            env: None,
            stdin: None,
        }
    }

    pub fn with_env(mut self, env: BTreeMap<String, String>) -> Self {
        self.env = Some(env);
        self
    }

    pub fn with_stdin(mut self, stdin: String) -> Self {
        self.stdin = Some(stdin);
        self
    }

    fn display(&self) -> String {
        std::iter::once(self.program.as_str())
            .chain(self.args.iter().map(String::as_str))
            .collect::<Vec<_>>()
            .join(" ")
    }
}

/// 標準出力を取り込んだ実行結果
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Captured {
    pub success: bool,
    pub stdout: String,
}

pub trait Runner {
    /// 標準出力を取り込んで実行する (git の問い合わせ用)。起動できなければ Err
    fn capture(&self, cmd: &Cmd) -> Result<Captured>;
    /// 出力を端末へそのまま流して実行し、失敗 (非 0 終了) は Err にする
    fn run(&self, cmd: &Cmd) -> Result<()>;
}

pub struct SystemRunner;

impl SystemRunner {
    fn command(cmd: &Cmd) -> Command {
        let mut command = Command::new(&cmd.program);
        command.args(&cmd.args);
        if let Some(env) = &cmd.env {
            command.env_clear().envs(env);
        }
        command
    }
}

impl Runner for SystemRunner {
    fn capture(&self, cmd: &Cmd) -> Result<Captured> {
        let out = Self::command(cmd)
            .stderr(Stdio::null())
            .output()
            .with_context(|| format!("{} を起動できません", cmd.program))?;
        Ok(Captured {
            success: out.status.success(),
            stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
        })
    }

    fn run(&self, cmd: &Cmd) -> Result<()> {
        let mut command = Self::command(cmd);
        if cmd.stdin.is_some() {
            command.stdin(Stdio::piped());
        }
        let mut child = command
            .spawn()
            .with_context(|| format!("{} を起動できません", cmd.program))?;
        if let Some(input) = &cmd.stdin {
            let mut pipe = child.stdin.take().expect("stdin は piped");
            pipe.write_all(input.as_bytes())?;
        }
        let status = child.wait()?;
        if !status.success() {
            // 引数に secret は載せない (password は stdin で渡す) ので表示してよい
            bail!("コマンドが失敗しました ({status}): {}", cmd.display());
        }
        Ok(())
    }
}

#[cfg(test)]
pub mod fake {
    use std::cell::RefCell;

    use super::*;

    /// 呼ばれたコマンドを記録し、`capture` には登録した応答を返す
    #[derive(Default)]
    pub struct FakeRunner {
        pub calls: RefCell<Vec<Cmd>>,
        /// (program, 先頭の引数) → 応答。未登録なら起動失敗扱い
        pub responses: Vec<((String, String), Captured)>,
    }

    impl FakeRunner {
        pub fn respond(
            mut self,
            program: &str,
            first_arg: &str,
            success: bool,
            stdout: &str,
        ) -> Self {
            self.responses.push((
                (program.to_string(), first_arg.to_string()),
                Captured {
                    success,
                    stdout: stdout.to_string(),
                },
            ));
            self
        }
    }

    impl Runner for FakeRunner {
        fn capture(&self, cmd: &Cmd) -> Result<Captured> {
            self.calls.borrow_mut().push(cmd.clone());
            let key = (
                cmd.program.clone(),
                cmd.args.first().cloned().unwrap_or_default(),
            );
            match self.responses.iter().find(|(k, _)| *k == key) {
                Some((_, captured)) => Ok(captured.clone()),
                None => bail!("{} を起動できません", cmd.program),
            }
        }

        fn run(&self, cmd: &Cmd) -> Result<()> {
            self.calls.borrow_mut().push(cmd.clone());
            Ok(())
        }
    }
}
