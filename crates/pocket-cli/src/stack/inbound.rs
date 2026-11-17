//! SES 受信の stack (Python 版 `pocket_cli/resources/inbound_template.py` と
//! `InboundStack` / `InboundDomainStack`)。
//!
//! この 2 種は Jinja2 の template を持たず、dict を組んで `json.dumps(indent=2)` した
//! 文字列が template になる。key の順まで Python 版に合わせる。

use magic_pocket_settings::context::{InboundContext, InboundDomainContext};
use magic_pocket_settings::pyjson;
use serde_json::{json, Map, Value as Json};

use super::StackTemplate;

const DKIM_TOKENS: [u8; 3] = [1, 2, 3];

fn sub(value: impl Into<String>) -> Json {
    json!({"Fn::Sub": value.into()})
}

fn reference(name: &str) -> Json {
    json!({"Ref": name})
}

fn arn(name: &str) -> Json {
    json!({"Fn::GetAtt": [name, "Arn"]})
}

fn statement(principal: Json, action: &str, resource: Json, condition: Json) -> Json {
    json!({
        "Effect": "Allow",
        "Principal": principal,
        "Action": action,
        "Resource": resource,
        "Condition": condition,
    })
}

fn policy(statements: Vec<Json>) -> Json {
    json!({"Version": "2012-10-17", "Statement": statements})
}

/// Python の `str(bool).lower()`
fn lower_bool(value: bool) -> &'static str {
    if value {
        "true"
    } else {
        "false"
    }
}

fn stack(name: &str, region: &str, template: &Json) -> StackTemplate {
    StackTemplate {
        name: name.to_string(),
        region: region.to_string(),
        capabilities: Vec::new(),
        tags: Vec::new(),
        export: Map::new(),
        body: pyjson::dumps_indent2(template),
    }
}

/// 受信口 (bucket / topic / queue policy / receipt rule) の stack。
///
/// `rule_set` / `after`: receipt rule を置く rule set と、直前の rule の名前
/// (active な rule set から決める。先頭に置くなら `None`)
pub fn inbound_stack(ctx: &InboundContext, rule_set: &str, after: Option<&str>) -> StackTemplate {
    stack(
        &ctx.resource_name,
        &ctx.region,
        &inbound_template(ctx, rule_set, after),
    )
}

/// 受信ドメイン (identity / DKIM / MX) の stack。
///
/// `hosted_zone_id`: manage_dns のときの hosted zone。`None` なら identity だけを作り、
/// 登録すべきレコードは Outputs から案内する
pub fn inbound_domain_stack(
    ctx: &InboundDomainContext,
    hosted_zone_id: Option<&str>,
) -> StackTemplate {
    stack(
        &ctx.stack_name,
        &ctx.region,
        &inbound_domain_template(ctx, hosted_zone_id),
    )
}

fn inbound_template(ctx: &InboundContext, rule_set: &str, after: Option<&str>) -> Json {
    let config = &ctx.config;
    let name = &ctx.resource_name;
    let after = after.filter(|a| !a.is_empty());
    let rule_arn = sub(format!(
        "arn:${{AWS::Partition}}:ses:${{AWS::Region}}:${{AWS::AccountId}}:\
         receipt-rule-set/{rule_set}:receipt-rule/{name}"
    ));
    let ses_condition = json!({
        "StringEquals": {"AWS:SourceAccount": reference("AWS::AccountId")},
        "ArnEquals": {"AWS:SourceArn": rule_arn},
    });
    let queue_base = "https://sqs.${AWS::Region}.${AWS::URLSuffix}/${AWS::AccountId}/";
    let queue_url = sub(format!("{queue_base}{}", ctx.queue_name));
    let lifecycle_rules: Vec<Json> = [
        config.raw_prefix.as_str(),
        config.metadata_prefix.as_str(),
        config.import_prefix.as_str(),
        "attachments/",
    ]
    .iter()
    .enumerate()
    .map(|(i, prefix)| {
        json!({
            "Id": format!("Expire{i}"),
            "Status": "Enabled",
            "Prefix": prefix,
            "ExpirationInDays": config.retention_days,
            "NoncurrentVersionExpiration": {"NoncurrentDays": config.retention_days},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        })
    })
    .collect();

    let mut resources = Map::new();
    resources.insert(
        "Bucket".into(),
        json!({
            "Type": "AWS::S3::Bucket",
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
            "Properties": {
                "BucketName": sub(ctx.bucket_name.clone()),
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": true,
                    "BlockPublicPolicy": true,
                    "IgnorePublicAcls": true,
                    "RestrictPublicBuckets": true,
                },
                "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
                "VersioningConfiguration": {"Status": "Enabled"},
                "BucketEncryption": {
                    "ServerSideEncryptionConfiguration": [
                        {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                    ]
                },
                "LifecycleConfiguration": {"Rules": lifecycle_rules},
            },
        }),
    );
    resources.insert(
        "BucketPolicy".into(),
        json!({
            "Type": "AWS::S3::BucketPolicy",
            "Properties": {
                "Bucket": reference("Bucket"),
                "PolicyDocument": policy(vec![
                    statement(
                        json!({"Service": "ses.amazonaws.com"}),
                        "s3:PutObject",
                        sub(format!(
                            "arn:${{AWS::Partition}}:s3:::${{Bucket}}/{}*",
                            config.raw_prefix
                        )),
                        ses_condition.clone(),
                    ),
                    json!({
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": "s3:*",
                        "Resource": [arn("Bucket"), sub("${Bucket.Arn}/*")],
                        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                    }),
                ]),
            },
        }),
    );
    resources.insert(
        "Topic".into(),
        json!({"Type": "AWS::SNS::Topic", "Properties": {"TopicName": name}}),
    );
    resources.insert(
        "TopicPolicy".into(),
        json!({
            "Type": "AWS::SNS::TopicPolicy",
            "Properties": {
                "Topics": [reference("Topic")],
                "PolicyDocument": policy(vec![statement(
                    json!({"Service": "ses.amazonaws.com"}),
                    "sns:Publish",
                    reference("Topic"),
                    ses_condition,
                )]),
            },
        }),
    );
    resources.insert(
        "DeliveryDLQ".into(),
        json!({
            "Type": "AWS::SQS::Queue",
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
            "Properties": {
                "QueueName": format!("{name}-delivery-dead-letter"),
                "MessageRetentionPeriod": 1209600,
                "SqsManagedSseEnabled": true,
            },
        }),
    );
    resources.insert(
        "QueuePolicy".into(),
        json!({
            "Type": "AWS::SQS::QueuePolicy",
            "Properties": {
                "Queues": [queue_url.clone(), reference("DeliveryDLQ")],
                "PolicyDocument": policy(vec![statement(
                    json!({"Service": "sns.amazonaws.com"}),
                    "sqs:SendMessage",
                    json!([sub(ctx.queue_arn.clone()), arn("DeliveryDLQ")]),
                    json!({"ArnEquals": {"aws:SourceArn": reference("Topic")}}),
                )]),
            },
        }),
    );
    resources.insert(
        "Subscription".into(),
        json!({
            "Type": "AWS::SNS::Subscription",
            "DependsOn": "QueuePolicy",
            "Properties": {
                "TopicArn": reference("Topic"),
                "Protocol": "sqs",
                "Endpoint": sub(ctx.queue_arn.clone()),
                "RawMessageDelivery": false,
                "RedrivePolicy": {"deadLetterTargetArn": arn("DeliveryDLQ")},
            },
        }),
    );

    let mut rule = json!({
        "RuleSetName": rule_set,
        "Rule": {
            "Name": name,
            "Enabled": config.enabled,
            "TlsPolicy": config.tls_policy,
            "ScanEnabled": config.scan_enabled,
            "Recipients": config.recipients,
            "Actions": [
                {
                    "S3Action": {
                        "BucketName": reference("Bucket"),
                        "ObjectKeyPrefix": config.raw_prefix,
                        "TopicArn": reference("Topic"),
                    }
                }
            ],
        },
    });
    if let Some(after) = after {
        rule["After"] = json!(after);
    }

    let mut outputs = Map::new();
    let mut output = |key: &str, value: Json| {
        outputs.insert(key.to_string(), json!({"Value": value}));
    };
    output("RuleSet", json!(rule_set));
    output("RuleName", json!(name));
    output("AfterRule", json!(after.unwrap_or_default()));
    output("Bucket", reference("Bucket"));
    output("TopicArn", reference("Topic"));
    output("QueueUrl", queue_url);
    output("DeliveryDLQUrl", reference("DeliveryDLQ"));
    output(
        "WorkerDLQUrl",
        sub(format!("{queue_base}{}-dead-letter", ctx.queue_name)),
    );
    output("Enabled", json!(lower_bool(config.enabled)));
    output("Handler", json!(config.handler));
    output("RawPrefix", json!(config.raw_prefix));
    output("MetadataPrefix", json!(config.metadata_prefix));
    output("ImportPrefix", json!(config.import_prefix));

    // Python は ReceiptRule を入れた後に alarm を足すので、Resources の順は
    // ReceiptRule → AlertTopic → alarm。ReceiptRule の DependsOn には alarm も入る
    let mut alarms = Map::new();
    add_alarms(&mut alarms, &mut outputs, ctx);
    let mut depends_on: Vec<String> =
        ["BucketPolicy", "TopicPolicy", "QueuePolicy", "Subscription"]
            .iter()
            .map(|s| s.to_string())
            .collect();
    depends_on.extend(alarms.keys().filter(|k| k.ends_with("Alarm")).cloned());
    resources.insert(
        "ReceiptRule".into(),
        json!({
            "Type": "AWS::SES::ReceiptRule",
            "DependsOn": depends_on,
            "Properties": rule,
        }),
    );
    resources.extend(alarms);

    json!({
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": resources,
        "Outputs": outputs,
    })
}

/// 配送の異常を知らせる alarm (delivery_alert が有効なとき)
fn add_alarms(
    resources: &mut Map<String, Json>,
    outputs: &mut Map<String, Json>,
    ctx: &InboundContext,
) {
    let alert = &ctx.config.delivery_alert;
    if !alert.enabled {
        return;
    }
    let email = alert.email.as_deref().filter(|e| !e.is_empty());
    if let Some(email) = email {
        resources.insert(
            "AlertTopic".into(),
            json!({
                "Type": "AWS::SNS::Topic",
                "Properties": {
                    "TopicName": format!("{}-alert", ctx.resource_name),
                    "Subscription": [{"Protocol": "email", "Endpoint": email}],
                },
            }),
        );
    }
    let delivery_dlq = format!("{}-delivery-dead-letter", ctx.resource_name);
    // (key, namespace, metric, dimension, 値, statistic, threshold)
    let metrics: [(&str, &str, &str, &str, &str, &str, i64); 4] = [
        (
            "PublishFailure",
            "AWS/SES",
            "PublishFailure",
            "RuleName",
            &ctx.resource_name,
            "Sum",
            0,
        ),
        (
            "PublishExpired",
            "AWS/SES",
            "PublishExpired",
            "RuleName",
            &ctx.resource_name,
            "Sum",
            0,
        ),
        (
            "DeliveryDLQ",
            "AWS/SQS",
            "ApproximateNumberOfMessagesVisible",
            "QueueName",
            &delivery_dlq,
            "Maximum",
            0,
        ),
        (
            "QueueAge",
            "AWS/SQS",
            "ApproximateAgeOfOldestMessage",
            "QueueName",
            &ctx.queue_name,
            "Maximum",
            3600,
        ),
    ];
    for (key, namespace, metric, dimension, value, statistic, threshold) in metrics {
        let logical = format!("{key}Alarm");
        let mut properties = json!({
            "AlarmName": format!("{}-{key}", ctx.resource_name),
            "Namespace": namespace,
            "MetricName": metric,
            "Dimensions": [{"Name": dimension, "Value": value}],
            "Statistic": statistic,
            "Period": 300,
            "EvaluationPeriods": 1,
            "Threshold": threshold,
            "ComparisonOperator": "GreaterThanThreshold",
            "TreatMissingData": "notBreaching",
        });
        if email.is_some() {
            properties["AlarmActions"] = json!([reference("AlertTopic")]);
            properties["OKActions"] = json!([reference("AlertTopic")]);
        }
        resources.insert(
            logical.clone(),
            json!({"Type": "AWS::CloudWatch::Alarm", "Properties": properties}),
        );
        outputs.insert(format!("{logical}Arn"), json!({"Value": arn(&logical)}));
    }
}

/// 受信ドメインの所有を 1 つの stack に閉じる。Easy DKIM の token は Fn::GetAtt で
/// 取れるため、検証用 CNAME も同じ stack で作れる
fn inbound_domain_template(ctx: &InboundDomainContext, hosted_zone_id: Option<&str>) -> Json {
    let hosted_zone_id = hosted_zone_id.filter(|id| !id.is_empty());
    let mut resources = Map::new();
    resources.insert(
        "Identity".into(),
        json!({
            "Type": "AWS::SES::EmailIdentity",
            "Properties": {
                "EmailIdentity": ctx.domain,
                "DkimSigningAttributes": {"NextSigningKeyLength": "RSA_2048_BIT"},
            },
        }),
    );
    let mut outputs = Map::new();
    outputs.insert("Domain".into(), json!({"Value": ctx.domain}));
    outputs.insert(
        "ManageDns".into(),
        json!({"Value": lower_bool(ctx.manage_dns)}),
    );
    outputs.insert("MxValue".into(), json!({"Value": ctx.mx_value}));
    for i in DKIM_TOKENS {
        let token_name = json!({"Fn::GetAtt": ["Identity", format!("DkimDNSTokenName{i}")]});
        let token_value = json!({"Fn::GetAtt": ["Identity", format!("DkimDNSTokenValue{i}")]});
        outputs.insert(format!("DkimName{i}"), json!({"Value": token_name}));
        outputs.insert(format!("DkimValue{i}"), json!({"Value": token_value}));
        if let Some(zone) = hosted_zone_id {
            resources.insert(
                format!("DkimRecord{i}"),
                json!({
                    "Type": "AWS::Route53::RecordSet",
                    "Properties": {
                        "HostedZoneId": zone,
                        "Name": token_name,
                        "Type": "CNAME",
                        "TTL": "1800",
                        "ResourceRecords": [token_value],
                    },
                }),
            );
        }
    }
    if let Some(zone) = hosted_zone_id {
        // 同名の MX が既にあれば stack 作成が失敗する (= 稼働中の配送先を奪わない)
        resources.insert(
            "MxRecord".into(),
            json!({
                "Type": "AWS::Route53::RecordSet",
                "Properties": {
                    "HostedZoneId": zone,
                    "Name": ctx.domain,
                    "Type": "MX",
                    "TTL": "300",
                    "ResourceRecords": [ctx.mx_value],
                },
            }),
        );
    }
    json!({
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": resources,
        "Outputs": outputs,
    })
}
