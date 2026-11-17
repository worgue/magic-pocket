//! inbound の受信ルールの置き場所 (Python 版 `resources/inbound.py` の `resolve_rules`)。
//!
//! receipt rule set は account / region で 1 つしか active にできない共有物なので、
//! pocket は active な set に自分のルールを差し込む。template にはその set の名前と
//! 「どのルールの後ろか」が入るため、status の hash 照合にも同じ解決が要る

use anyhow::{bail, Result};
use magic_pocket_settings::context::InboundContext;

use crate::stack::StackOutputs;

/// active な receipt rule set (`describe_active_receipt_rule_set`)
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ActiveRuleSet {
    /// active な set が無ければ `None`
    pub name: Option<String>,
    pub rules: Vec<ReceiptRule>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ReceiptRule {
    pub name: String,
    pub enabled: bool,
    pub recipients: Vec<String>,
    /// 後続のルールを止める action (Stop / Bounce / Lambda) を持つか
    pub stopping: bool,
}

/// active set が無い account / region 向けの案内
pub fn no_active_rule_set_guide(region: &str) -> String {
    format!(
        "active な receipt rule set がありません。\
         account/regionで1回だけ次を実行してください\
         （account単位の土台なので、pocketは作成・有効化しません）:\n  \
         aws ses create-receipt-rule-set --rule-set-name default-rule-set --region {region}\n  \
         aws ses set-active-receipt-rule-set --rule-set-name default-rule-set --region {region}"
    )
}

/// 受信ルールの宛先の指定 (`pattern`) が address に当たるか
pub fn matches_address(pattern: &str, address: &str) -> bool {
    let pattern = pattern.to_lowercase();
    let address = address.to_lowercase();
    let domain = address.split_once('@').map_or("", |(_, domain)| domain);
    if pattern.contains('@') {
        pattern == address
    } else if pattern.starts_with('.') {
        domain.ends_with(&pattern)
    } else {
        domain == pattern
    }
}

/// 差し込み先を決める。戻り値は (rule set 名, 直前のルール名)。
/// `deployed` は deploy 済みの inbound stack の output
pub fn resolve_rules(
    active: &ActiveRuleSet,
    ctx: &InboundContext,
    deployed: Option<&StackOutputs>,
) -> Result<(String, Option<String>)> {
    let Some(name) = active.name.as_deref().filter(|n| !n.is_empty()) else {
        bail!("{}", no_active_rule_set_guide(&ctx.region));
    };
    let declared = ctx.config.rule_set.as_deref().filter(|r| !r.is_empty());
    if declared.is_some_and(|rule_set| rule_set != name) {
        bail!("明示したrule_setがactive setと一致しません");
    }
    let deployed = deployed.filter(|output| !output.is_empty());
    let expected = deployed
        .and_then(|output| output.get("RuleSet"))
        .map(String::as_str)
        .filter(|r| !r.is_empty())
        .or(declared);
    if let Some(expected) = expected.filter(|expected| *expected != name) {
        bail!("active rule set が {expected} から {name} に変わっています");
    }
    let own = active
        .rules
        .iter()
        .position(|rule| rule.name == ctx.resource_name);
    if deployed.is_some() && own.is_none() {
        bail!("管理中の受信ルールが見つかりません。削除・移動を確認してください");
    }
    if own.is_some() && deployed.is_none() {
        bail!("同名の未管理ルールがあります。既存ルールを取り込みません");
    }
    for (index, rule) in active.rules.iter().enumerate() {
        if Some(index) == own || !rule.enabled {
            continue;
        }
        let overlap = rule.recipients.is_empty()
            || rule.recipients.iter().any(|pattern| {
                ctx.config
                    .recipients
                    .iter()
                    .any(|address| matches_address(pattern, address))
            });
        if rule.stopping || overlap {
            bail!(
                "既存ルール {} と競合します（宛先重複または停止・Lambda処理）",
                rule.name
            );
        }
    }
    let after = match own {
        Some(0) => None,
        Some(index) => Some(active.rules[index - 1].name.clone()),
        None => active.rules.last().map(|rule| rule.name.clone()),
    };
    Ok((name.to_string(), after))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::vector_context;

    fn inlet() -> InboundContext {
        let ctx = vector_context("example/example-dsql.sandbox");
        let inlet = ctx.inbound.values().next().unwrap().clone();
        inlet
    }

    fn rule(name: &str, recipients: &[&str]) -> ReceiptRule {
        ReceiptRule {
            name: name.to_string(),
            enabled: true,
            recipients: recipients.iter().map(|r| r.to_string()).collect(),
            stopping: false,
        }
    }

    fn active(rules: Vec<ReceiptRule>) -> ActiveRuleSet {
        ActiveRuleSet {
            name: Some("default-rule-set".to_string()),
            rules,
        }
    }

    fn deployed(rule_set: &str) -> StackOutputs {
        StackOutputs::from([("RuleSet".to_string(), rule_set.to_string())])
    }

    fn error(result: Result<(String, Option<String>)>) -> String {
        result.unwrap_err().to_string()
    }

    #[test]
    fn test_matches_address() {
        assert!(matches_address("A@Example.com", "a@example.COM"));
        assert!(!matches_address("b@example.com", "a@example.com"));
        assert!(matches_address("example.com", "a@example.com"));
        assert!(!matches_address("example.com", "a@sub.example.com"));
        assert!(matches_address(".example.com", "a@sub.example.com"));
        assert!(!matches_address(".example.com", "a@example.com"));
    }

    #[test]
    fn test_first_deploy_appends_after_last_rule() {
        let ctx = inlet();
        let set = active(vec![rule("other", &["x@other.example.net"])]);
        assert_eq!(
            resolve_rules(&set, &ctx, None).unwrap(),
            ("default-rule-set".to_string(), Some("other".to_string()))
        );
        assert_eq!(
            resolve_rules(&active(vec![]), &ctx, None).unwrap(),
            ("default-rule-set".to_string(), None)
        );
    }

    #[test]
    fn test_deployed_rule_keeps_its_position() {
        let ctx = inlet();
        let own = rule(&ctx.resource_name, &[]);
        let other = rule("other", &["x@other.example.net"]);
        let output = deployed("default-rule-set");
        let first = active(vec![own.clone(), other.clone()]);
        assert_eq!(resolve_rules(&first, &ctx, Some(&output)).unwrap().1, None);
        let second = active(vec![other, own]);
        assert_eq!(
            resolve_rules(&second, &ctx, Some(&output)).unwrap().1,
            Some("other".to_string())
        );
    }

    #[test]
    fn test_errors() {
        let ctx = inlet();
        let own = rule(&ctx.resource_name, &[]);
        let none = ActiveRuleSet::default();
        assert!(error(resolve_rules(&none, &ctx, None))
            .starts_with("active な receipt rule set がありません。"));
        let mut declared = ctx.clone();
        declared.config.rule_set = Some("mine".to_string());
        assert_eq!(
            error(resolve_rules(&active(vec![]), &declared, None)),
            "明示したrule_setがactive setと一致しません"
        );
        assert_eq!(
            error(resolve_rules(
                &active(vec![own.clone()]),
                &ctx,
                Some(&deployed("old-set"))
            )),
            "active rule set が old-set から default-rule-set に変わっています"
        );
        assert_eq!(
            error(resolve_rules(
                &active(vec![]),
                &ctx,
                Some(&deployed("default-rule-set"))
            )),
            "管理中の受信ルールが見つかりません。削除・移動を確認してください"
        );
        assert_eq!(
            error(resolve_rules(&active(vec![own]), &ctx, None)),
            "同名の未管理ルールがあります。既存ルールを取り込みません"
        );
    }

    #[test]
    fn test_conflicting_rules() {
        let ctx = inlet();
        let message = "既存ルール other と競合します（宛先重複または停止・Lambda処理）";
        // 宛先なし (全受信)・宛先の重複・後続を止める action
        let catch_all = rule("other", &[]);
        assert_eq!(
            error(resolve_rules(&active(vec![catch_all]), &ctx, None)),
            message
        );
        let same = rule("other", &[ctx.config.recipients[0].as_str()]);
        assert_eq!(
            error(resolve_rules(&active(vec![same]), &ctx, None)),
            message
        );
        let mut stopping = rule("other", &["x@other.example.net"]);
        stopping.stopping = true;
        assert_eq!(
            error(resolve_rules(&active(vec![stopping.clone()]), &ctx, None)),
            message
        );
        // 無効なルールは見ない
        stopping.enabled = false;
        assert!(resolve_rules(&active(vec![stopping]), &ctx, None).is_ok());
    }
}
