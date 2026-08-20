//! SeaORM エンティティ。
//!
//! 本来は `_dsql_lab` (schema.sql から再構築したローカル PG) から
//! `just entity-gen` で生成する (dsql-schema-migrate のフロー)。この example は
//! テーブルが 1 つだけなので、生成結果に相当するものを直接置いている。

pub mod messages;

pub use messages::Entity as Messages;
