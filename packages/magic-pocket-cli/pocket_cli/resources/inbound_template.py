"""SES受信用テンプレート。受信ルールは配送経路の完成後に有効化する。"""

from pocket.inbound_context import InboundContext


def sub(value: str) -> dict:
    return {"Fn::Sub": value}


def ref(name: str) -> dict:
    return {"Ref": name}


def arn(name: str) -> dict:
    return {"Fn::GetAtt": [name, "Arn"]}


def statement(principal, action, resource, condition=None) -> dict:
    result = {
        "Effect": "Allow",
        "Principal": principal,
        "Action": action,
        "Resource": resource,
    }
    if condition:
        result["Condition"] = condition
    return result


def policy(statements: list[dict]) -> dict:
    return {"Version": "2012-10-17", "Statement": statements}


def build_template(ctx: InboundContext, rule_set: str, after: str | None) -> dict:
    config = ctx.config
    name = ctx.resource_name
    rule_arn = sub(
        "arn:${AWS::Partition}:ses:${AWS::Region}:${AWS::AccountId}:"
        f"receipt-rule-set/{rule_set}:receipt-rule/{name}"
    )
    ses_condition = {
        "StringEquals": {"AWS:SourceAccount": ref("AWS::AccountId")},
        "ArnEquals": {"AWS:SourceArn": rule_arn},
    }
    queue_url = sub(
        "https://sqs.${AWS::Region}.${AWS::URLSuffix}/${AWS::AccountId}/"
        + ctx.queue_name
    )
    resources = {
        "Bucket": {
            "Type": "AWS::S3::Bucket",
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
            "Properties": {
                "BucketName": sub(ctx.bucket_name),
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True,
                    "RestrictPublicBuckets": True,
                },
                "OwnershipControls": {
                    "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
                },
                "VersioningConfiguration": {"Status": "Enabled"},
                "BucketEncryption": {
                    "ServerSideEncryptionConfiguration": [
                        {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                    ]
                },
                "LifecycleConfiguration": {
                    "Rules": [
                        {
                            "Id": f"Expire{i}",
                            "Status": "Enabled",
                            "Prefix": prefix,
                            "ExpirationInDays": config.retention_days,
                            "NoncurrentVersionExpiration": {
                                "NoncurrentDays": config.retention_days
                            },
                            "AbortIncompleteMultipartUpload": {
                                "DaysAfterInitiation": 7
                            },
                        }
                        for i, prefix in enumerate(
                            [
                                config.raw_prefix,
                                config.metadata_prefix,
                                config.import_prefix,
                                "attachments/",
                            ]
                        )
                    ]
                },
            },
        },
        "BucketPolicy": {
            "Type": "AWS::S3::BucketPolicy",
            "Properties": {
                "Bucket": ref("Bucket"),
                "PolicyDocument": policy(
                    [
                        statement(
                            {"Service": "ses.amazonaws.com"},
                            "s3:PutObject",
                            sub(
                                "arn:${AWS::Partition}:s3:::${Bucket}/"
                                + config.raw_prefix
                                + "*"
                            ),
                            ses_condition,
                        ),
                        {
                            "Effect": "Deny",
                            "Principal": "*",
                            "Action": "s3:*",
                            "Resource": [arn("Bucket"), sub("${Bucket.Arn}/*")],
                            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                        },
                    ]
                ),
            },
        },
        "Topic": {"Type": "AWS::SNS::Topic", "Properties": {"TopicName": name}},
        "TopicPolicy": {
            "Type": "AWS::SNS::TopicPolicy",
            "Properties": {
                "Topics": [ref("Topic")],
                "PolicyDocument": policy(
                    [
                        statement(
                            {"Service": "ses.amazonaws.com"},
                            "sns:Publish",
                            ref("Topic"),
                            ses_condition,
                        )
                    ]
                ),
            },
        },
        "DeliveryDLQ": {
            "Type": "AWS::SQS::Queue",
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
            "Properties": {
                "QueueName": name + "-delivery-dead-letter",
                "MessageRetentionPeriod": 1209600,
                "SqsManagedSseEnabled": True,
            },
        },
        "QueuePolicy": {
            "Type": "AWS::SQS::QueuePolicy",
            "Properties": {
                "Queues": [queue_url, ref("DeliveryDLQ")],
                "PolicyDocument": policy(
                    [
                        statement(
                            {"Service": "sns.amazonaws.com"},
                            "sqs:SendMessage",
                            [sub(ctx.queue_arn), arn("DeliveryDLQ")],
                            {"ArnEquals": {"aws:SourceArn": ref("Topic")}},
                        )
                    ]
                ),
            },
        },
        "Subscription": {
            "Type": "AWS::SNS::Subscription",
            "DependsOn": "QueuePolicy",
            "Properties": {
                "TopicArn": ref("Topic"),
                "Protocol": "sqs",
                "Endpoint": sub(ctx.queue_arn),
                "RawMessageDelivery": False,
                "RedrivePolicy": {"deadLetterTargetArn": arn("DeliveryDLQ")},
            },
        },
    }
    rule = {
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
                        "BucketName": ref("Bucket"),
                        "ObjectKeyPrefix": config.raw_prefix,
                        "TopicArn": ref("Topic"),
                    }
                }
            ],
        },
    }
    if after:
        rule["After"] = after
    resources["ReceiptRule"] = {
        "Type": "AWS::SES::ReceiptRule",
        "DependsOn": ["BucketPolicy", "TopicPolicy", "QueuePolicy", "Subscription"],
        "Properties": rule,
    }
    outputs = {
        "RuleSet": {"Value": rule_set},
        "RuleName": {"Value": name},
        "AfterRule": {"Value": after or ""},
        "Bucket": {"Value": ref("Bucket")},
        "TopicArn": {"Value": ref("Topic")},
        "QueueUrl": {"Value": queue_url},
        "DeliveryDLQUrl": {"Value": ref("DeliveryDLQ")},
        "WorkerDLQUrl": {
            "Value": sub(
                "https://sqs.${AWS::Region}.${AWS::URLSuffix}/${AWS::AccountId}/"
                + ctx.queue_name
                + "-dead-letter"
            )
        },
    }
    outputs["Enabled"] = {"Value": str(config.enabled).lower()}
    outputs["Handler"] = {"Value": config.handler}
    outputs["RawPrefix"] = {"Value": config.raw_prefix}
    outputs["MetadataPrefix"] = {"Value": config.metadata_prefix}
    outputs["ImportPrefix"] = {"Value": config.import_prefix}
    add_alarms(resources, outputs, ctx)
    resources["ReceiptRule"]["DependsOn"].extend(
        key for key in resources if key.endswith("Alarm")
    )
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": resources,
        "Outputs": outputs,
    }


def add_alarms(resources: dict, outputs: dict, ctx: InboundContext) -> None:
    alert = ctx.config.delivery_alert
    if not alert.enabled:
        return
    if alert.email:
        resources["AlertTopic"] = {
            "Type": "AWS::SNS::Topic",
            "Properties": {
                "TopicName": ctx.resource_name + "-alert",
                "Subscription": [{"Protocol": "email", "Endpoint": alert.email}],
            },
        }
    metrics = [
        (
            "PublishFailure",
            "AWS/SES",
            "PublishFailure",
            "RuleName",
            ctx.resource_name,
            "Sum",
            0,
        ),
        (
            "PublishExpired",
            "AWS/SES",
            "PublishExpired",
            "RuleName",
            ctx.resource_name,
            "Sum",
            0,
        ),
        (
            "DeliveryDLQ",
            "AWS/SQS",
            "ApproximateNumberOfMessagesVisible",
            "QueueName",
            ctx.resource_name + "-delivery-dead-letter",
            "Maximum",
            0,
        ),
        (
            "QueueAge",
            "AWS/SQS",
            "ApproximateAgeOfOldestMessage",
            "QueueName",
            ctx.queue_name,
            "Maximum",
            3600,
        ),
    ]
    for key, namespace, metric, dimension, value, statistic, threshold in metrics:
        logical = key + "Alarm"
        properties = {
            "AlarmName": ctx.resource_name + "-" + key,
            "Namespace": namespace,
            "MetricName": metric,
            "Dimensions": [{"Name": dimension, "Value": value}],
            "Statistic": statistic,
            "Period": 300,
            "EvaluationPeriods": 1,
            "Threshold": threshold,
            "ComparisonOperator": "GreaterThanThreshold",
            "TreatMissingData": "notBreaching",
        }
        if alert.email:
            properties.update(
                AlarmActions=[ref("AlertTopic")], OKActions=[ref("AlertTopic")]
            )
        resources[logical] = {
            "Type": "AWS::CloudWatch::Alarm",
            "Properties": properties,
        }
        outputs[logical + "Arn"] = {"Value": arn(logical)}
