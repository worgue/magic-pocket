//! 確認プロンプト (Python 版 `cli/interaction.py` と `click.confirm`)。

use std::io::{BufRead as _, Write as _};

use anyhow::{bail, Result};

/// 利用者への確認。テストでは答えを差し替える
pub trait Prompt {
    /// `-y` (assume yes) なら聞かずに `true`。そうでなければ聞く
    fn confirm(&self, text: &str, default: bool) -> Result<bool>;
    /// `-y` でも自動では承認しない確認 (データの削除など)。`-y` のときは聞かずに `false`
    fn confirm_explicit(&self, text: &str) -> Result<bool>;
    /// [`Prompt::confirm_explicit`] が実際に聞くか (`-y` なら聞かない)
    fn asks_explicitly(&self) -> bool;
}

/// 端末 (stdin) に聞く [`Prompt`]
pub struct Interaction {
    pub assume_yes: bool,
}

/// `text [Y/n]: ` と聞く。空の入力は `default`、入力が閉じていれば中止
fn ask(text: &str, default: bool) -> Result<bool> {
    let suffix = if default { "[Y/n]" } else { "[y/N]" };
    loop {
        print!("{text} {suffix}: ");
        std::io::stdout().flush()?;
        let mut line = String::new();
        if std::io::stdin().lock().read_line(&mut line)? == 0 {
            println!();
            bail!("Aborted!");
        }
        match parse_answer(&line, default) {
            Some(answer) => return Ok(answer),
            None => println!("Error: invalid input"),
        }
    }
}

/// click と同じ受け付け: y / yes / n / no (大文字小文字を問わない)。空は `default`
pub fn parse_answer(line: &str, default: bool) -> Option<bool> {
    match line.trim().to_ascii_lowercase().as_str() {
        "" => Some(default),
        "y" | "yes" => Some(true),
        "n" | "no" => Some(false),
        _ => None,
    }
}

impl Prompt for Interaction {
    fn confirm(&self, text: &str, default: bool) -> Result<bool> {
        if self.assume_yes {
            return Ok(true);
        }
        ask(text, default)
    }

    fn confirm_explicit(&self, text: &str) -> Result<bool> {
        if self.assume_yes {
            return Ok(false);
        }
        ask(text, false)
    }

    fn asks_explicitly(&self) -> bool {
        !self.assume_yes
    }
}

#[cfg(test)]
pub mod testing {
    use std::cell::RefCell;

    use super::*;

    /// 決めた答えを返し、聞かれた文言を記録する
    pub struct FakePrompt {
        pub assume_yes: bool,
        /// `-y` でないときの答え
        pub answer: bool,
        pub asked: RefCell<Vec<String>>,
    }

    impl FakePrompt {
        pub fn yes() -> Self {
            Self {
                assume_yes: true,
                answer: true,
                asked: RefCell::default(),
            }
        }

        pub fn answering(answer: bool) -> Self {
            Self {
                assume_yes: false,
                answer,
                asked: RefCell::default(),
            }
        }
    }

    impl Prompt for FakePrompt {
        fn confirm(&self, text: &str, _default: bool) -> Result<bool> {
            if self.assume_yes {
                return Ok(true);
            }
            self.asked.borrow_mut().push(text.to_string());
            Ok(self.answer)
        }

        fn confirm_explicit(&self, text: &str) -> Result<bool> {
            if self.assume_yes {
                return Ok(false);
            }
            self.asked.borrow_mut().push(text.to_string());
            Ok(self.answer)
        }

        fn asks_explicitly(&self) -> bool {
            !self.assume_yes
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_answer() {
        assert_eq!(parse_answer("\n", true), Some(true));
        assert_eq!(parse_answer("\n", false), Some(false));
        assert_eq!(parse_answer(" Y \n", false), Some(true));
        assert_eq!(parse_answer("yes\n", false), Some(true));
        assert_eq!(parse_answer("No\n", true), Some(false));
        assert_eq!(parse_answer("maybe\n", true), None);
    }

    #[test]
    fn test_assume_yes_never_approves_explicit_confirmation() {
        let prompt = Interaction { assume_yes: true };
        assert!(prompt.confirm("消しますか？", false).unwrap());
        assert!(!prompt.confirm_explicit("データも消しますか？").unwrap());
    }
}
