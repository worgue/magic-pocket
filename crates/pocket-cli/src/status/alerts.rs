//! DLQ alert の email 購読の状態 (Python 版 `resources/sqs_alert.py`)。
//!
//! PendingConfirmation のままだと通知が届かないので、status の最後に一覧で出す

use magic_pocket_settings::context::Context;

/// 購読を確かめる対象 (topic と宛先)
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlertTarget {
    pub region: String,
    pub topic_name: String,
    pub email: String,
}

/// alert の宛先を持つ handler の DLQ と inbound を、Python 版と同じ順に並べる
pub fn targets(ctx: &Context) -> Vec<AlertTarget> {
    let mut list = Vec::new();
    for container in ctx.container.values() {
        for handler in container.handlers.values() {
            let alert = handler
                .sqs
                .as_ref()
                .and_then(|s| s.dead_letter_alert.as_ref());
            let Some(alert) = alert else {
                continue;
            };
            if let Some(email) = alert.email.as_deref().filter(|e| !e.is_empty()) {
                list.push(AlertTarget {
                    region: container.region.clone(),
                    topic_name: alert.topic_name.clone(),
                    email: email.to_string(),
                });
            }
        }
    }
    for inlet in ctx.inbound.values() {
        let alert = &inlet.config.delivery_alert;
        if !alert.enabled {
            continue;
        }
        if let Some(email) = alert.email.as_deref().filter(|e| !e.is_empty()) {
            list.push(AlertTarget {
                region: inlet.region.clone(),
                topic_name: format!("{}-alert", inlet.resource_name),
                email: email.to_string(),
            });
        }
    }
    list
}

/// deploy の最後に出す警告 (Python 版 `echo_dead_letter_alert_warnings`)。確認済みなら `None`
pub fn deploy_warning(target: &AlertTarget, topic_arn: &str, state: &str) -> Option<String> {
    let head = format!(
        "DLQ alert subscription for {} ({})",
        target.email, target.topic_name
    );
    Some(match state {
        "Confirmed" => return None,
        "PendingConfirmation" => format!(
            "{head} is still PendingConfirmation. Alarm notifications will NOT be \
             delivered until the confirmation link in the email is clicked."
        ),
        "Deleted" => format!(
            "{head} has been unsubscribed (SubscriptionArn=Deleted). Alarm \
             notifications will NOT be delivered. Re-subscribe the email and \
             confirm it again."
        ),
        "NotSubscribed" => format!(
            "{head} was not found on the topic. An unconfirmed subscription is removed \
             by SNS once its confirmation expires (3 days). Alarm notifications \
             will NOT be delivered. Re-subscribe and confirm it: aws sns \
             subscribe --topic-arn {topic_arn} --protocol email \
             --notification-endpoint {}",
            target.email
        ),
        _ => format!(
            "DLQ alert topic {} was not found. Check the container stack deployment.",
            target.topic_name
        ),
    })
}

/// `list_subscriptions_by_topic` の 1 件
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Subscription {
    pub protocol: String,
    pub endpoint: String,
    pub arn: String,
}

/// topic の購読の一覧から、宛先の購読の状態を決める。
///
/// 確定済みがあれば Confirmed。確認待ちは、確認済みを解除した跡 (Deleted) より優先する
pub fn subscription_state(subscriptions: &[Subscription], email: &str) -> &'static str {
    let mut state = "NotSubscribed";
    for subscription in subscriptions {
        if subscription.protocol != "email" || subscription.endpoint != email {
            continue;
        }
        if subscription.arn.starts_with("arn:") {
            return "Confirmed";
        }
        if subscription.arn == "PendingConfirmation" {
            state = "PendingConfirmation";
        } else if state == "NotSubscribed" {
            state = "Deleted";
        }
    }
    state
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stack::testing::vector_context;

    fn sub(protocol: &str, endpoint: &str, arn: &str) -> Subscription {
        Subscription {
            protocol: protocol.to_string(),
            endpoint: endpoint.to_string(),
            arn: arn.to_string(),
        }
    }

    #[test]
    fn test_subscription_state_table() {
        // Python 版 tests/test_sqs_dead_letter_alert.py と同じ場合分け
        let arn = "arn:aws:sns:ap-northeast-1:123456789012:t:abc";
        assert_eq!(subscription_state(&[], "a@example.com"), "NotSubscribed");
        assert_eq!(
            subscription_state(&[sub("email", "a@example.com", arn)], "a@example.com"),
            "Confirmed"
        );
        assert_eq!(
            subscription_state(
                &[sub("email", "a@example.com", "PendingConfirmation")],
                "a@example.com"
            ),
            "PendingConfirmation"
        );
        assert_eq!(
            subscription_state(&[sub("email", "a@example.com", "Deleted")], "a@example.com"),
            "Deleted"
        );
        // 確認待ちは Deleted に上書きされない (順序によらない)
        for subs in [
            [
                sub("email", "a@example.com", "PendingConfirmation"),
                sub("email", "a@example.com", "Deleted"),
            ],
            [
                sub("email", "a@example.com", "Deleted"),
                sub("email", "a@example.com", "PendingConfirmation"),
            ],
        ] {
            assert_eq!(
                subscription_state(&subs, "a@example.com"),
                "PendingConfirmation"
            );
        }
        // 別の宛先・別の protocol は見ない
        assert_eq!(
            subscription_state(
                &[
                    sub("email", "b@example.com", arn),
                    sub("https", "a@example.com", arn)
                ],
                "a@example.com"
            ),
            "NotSubscribed"
        );
    }

    #[test]
    fn test_targets_cover_handler_dlq_and_inbound() {
        let ctx = vector_context("example/example-dsql.sandbox");
        let list = targets(&ctx);
        let topics: Vec<&str> = list.iter().map(|t| t.topic_name.as_str()).collect();
        assert_eq!(list.len(), 3, "{topics:?}");
        // handler の DLQ が先、inbound が後
        assert!(topics[0].ends_with("-dead-letter-alert"), "{topics:?}");
        assert!(!topics[2].contains("dead-letter"), "{topics:?}");
        assert!(list.iter().all(|t| t.email == "alerts@example.com"));
    }

    #[test]
    fn test_no_targets_without_alert_email() {
        assert!(targets(&vector_context("toml/rds.dev")).is_empty());
    }

    #[test]
    fn test_deploy_warning_per_state() {
        let target = AlertTarget {
            region: "ap-northeast-1".to_string(),
            topic_name: "dev-app-dlq".to_string(),
            email: "ops@example.com".to_string(),
        };
        let arn = "arn:aws:sns:ap-northeast-1:123456789012:dev-app-dlq";
        assert_eq!(deploy_warning(&target, arn, "Confirmed"), None);
        assert_eq!(
            deploy_warning(&target, arn, "PendingConfirmation").unwrap(),
            "DLQ alert subscription for ops@example.com (dev-app-dlq) is still \
             PendingConfirmation. Alarm notifications will NOT be delivered until the \
             confirmation link in the email is clicked."
        );
        assert!(deploy_warning(&target, arn, "Deleted")
            .unwrap()
            .contains("has been unsubscribed (SubscriptionArn=Deleted)"));
        assert!(deploy_warning(&target, arn, "NotSubscribed")
            .unwrap()
            .ends_with(&format!(
                "aws sns subscribe --topic-arn {arn} --protocol email \
                 --notification-endpoint ops@example.com"
            )));
        assert_eq!(
            deploy_warning(&target, arn, "NotCreated").unwrap(),
            "DLQ alert topic dev-app-dlq was not found. Check the container stack deployment."
        );
    }
}
