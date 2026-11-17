//! CloudFormation stack (template の描画と stack の操作)。
//!
//! Python 版 `pocket_cli/resources/aws/cloudformation.py` の移植。stack の
//! `pocket:template_hash` タグは template のテキストの hash なので、描画結果は
//! Python 版とバイト一致させる (`tests/data/template_vectors` で固定している)。

pub mod render;

#[cfg(test)]
mod testing;
