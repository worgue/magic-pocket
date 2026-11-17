//! status の出力先。Python 版の `echo.*` (rich。すべて stderr) と `print` (stdout) に当たる。
//!
//! 判定の途中で出す警告も含めてすべてここを通すので、出力の順・文言・出力先を
//! 記録用の sink でテストできる。rich の折り返しと自動ハイライトは再現しない

use std::io::{IsTerminal, Write};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stream {
    Stdout,
    Stderr,
}

/// Python 版の `echo` の種類 (theme の色)。`Plain` は `print` (stdout、無色)
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Style {
    Success,
    Info,
    Warning,
    Danger,
    Log,
    Plain,
}

impl Style {
    /// rich の theme と同じ色 (success=green / info=cyan / warning=magenta /
    /// danger=bold red / log=dim)
    fn ansi(self) -> Option<&'static str> {
        match self {
            Style::Success => Some("32"),
            Style::Info => Some("36"),
            Style::Warning => Some("35"),
            Style::Danger => Some("1;31"),
            Style::Log => Some("2"),
            Style::Plain => None,
        }
    }
}

pub trait Sink {
    fn emit(&mut self, stream: Stream, style: Style, text: &str);

    fn success(&mut self, text: &str) {
        self.emit(Stream::Stderr, Style::Success, text);
    }
    fn info(&mut self, text: &str) {
        self.emit(Stream::Stderr, Style::Info, text);
    }
    fn warning(&mut self, text: &str) {
        self.emit(Stream::Stderr, Style::Warning, text);
    }
    fn danger(&mut self, text: &str) {
        self.emit(Stream::Stderr, Style::Danger, text);
    }
    fn log(&mut self, text: &str) {
        self.emit(Stream::Stderr, Style::Log, text);
    }
    /// Python の `print` (stdout)
    fn print(&mut self, text: &str) {
        self.emit(Stream::Stdout, Style::Plain, text);
    }
}

/// 端末への出力。色は stderr が端末のときだけ付ける
pub struct Terminal {
    color: bool,
}

impl Terminal {
    pub fn new() -> Self {
        Self {
            color: std::io::stderr().is_terminal() && std::env::var_os("NO_COLOR").is_none(),
        }
    }
}

impl Sink for Terminal {
    fn emit(&mut self, stream: Stream, style: Style, text: &str) {
        match stream {
            Stream::Stdout => println!("{text}"),
            Stream::Stderr => {
                // stdout と行が前後しないよう、先に stdout を流す
                let _ = std::io::stdout().flush();
                match style.ansi().filter(|_| self.color) {
                    Some(code) => eprintln!("\x1b[{code}m{text}\x1b[0m"),
                    None => eprintln!("{text}"),
                }
            }
        }
    }
}

/// テスト用: 出力を記録する
#[cfg(test)]
#[derive(Default)]
pub struct Recorded(pub Vec<(Stream, Style, String)>);

#[cfg(test)]
impl Sink for Recorded {
    fn emit(&mut self, stream: Stream, style: Style, text: &str) {
        self.0.push((stream, style, text.to_string()));
    }
}
