//! Settings から Context を組み立てる (Python の各 `from_settings` と
//! `Context.from_settings`)。外部入力の呼び出しと log の記録をここに集める。

use std::cell::RefCell;
use std::collections::BTreeMap;

use super::cloudfront::{bucket_policy_prefix, rstrip_slash_star, waf_path_match};
use super::general::{format_vars, parse_handler_ref};
use super::{
    capitalize, pyformat, user_secret_path, ApiGatewayContext, BackupContext, BackupPlanContext,
    BackupRuleContext, BuildContext, CloudFrontContext, CloudFrontWafAllowRuleContext,
    CloudFrontWafContext, CloudFrontWafRateLimitContext, ContainerContext, ContainerIamContext,
    Context, ContextError, ContextInputs, ContextMessage, DjangoContext, DsqlContext,
    GeneralContext, HostedZone, InboundContext, InboundDomainContext, LambdaHandlerContext,
    MessageLevel, NeonContext, RdsContext, RedirectFromContext, RouteContext, S3Context,
    S3CorsContext, S3LifecycleRuleContext, ScheduleEntryContext, SchedulerContext, SecretsContext,
    SesContext, SqsContext, SqsDeadLetterAlertContext, TiDbContext, VpcContext,
};
use crate::model::cloudfront::camel_logical_name;
use crate::model::{
    Backup, CloudFront, Container, LambdaHandler, ManagedSecretSpec, ManagedSecretType, OneOrMany,
    Route, RouteType, SchedulerKind, Secrets, Settings, StoreType, UserSecretSpec, Versioning,
};
use crate::types::{FormatStr, OrderedMap, StrOrInt};

/// enable_origin_verify 用 managed secret の予約キー
const ORIGIN_VERIFY_SECRET_KEY: &str = "POCKET_ORIGIN_VERIFY_SECRET";
/// `pocket.naming.DSQL_ENDPOINT`
const DSQL_ENDPOINT: &str = "dsql_endpoint";

pub(crate) struct Builder<'a> {
    s: &'a Settings,
    env: &'a BTreeMap<String, String>,
    inputs: &'a ContextInputs<'a>,
    messages: RefCell<Vec<ContextMessage>>,
    /// `get_hosted_zones` の @cache
    zones: RefCell<Option<Vec<HostedZone>>>,
    /// `get_hosted_zone_id_from_domain` の @cache
    zone_ids: RefCell<BTreeMap<String, String>>,
}

impl<'a> Builder<'a> {
    pub(crate) fn new(s: &'a Settings, inputs: &'a ContextInputs<'a>) -> Self {
        Self {
            s,
            env: inputs.env,
            inputs,
            messages: RefCell::new(Vec::new()),
            zones: RefCell::new(None),
            zone_ids: RefCell::new(BTreeMap::new()),
        }
    }

    pub(crate) fn into_messages(self) -> Vec<ContextMessage> {
        self.messages.into_inner()
    }

    fn log(&self, level: MessageLevel, message: impl Into<String>) {
        self.messages.borrow_mut().push(ContextMessage {
            level,
            message: message.into(),
        });
    }

    /// 環境変数 (空文字は未設定と同じ = Python の `os.environ.get(k) or ...`)
    fn env_nonempty(&self, key: &str) -> Option<String> {
        self.env.get(key).filter(|v| !v.is_empty()).cloned()
    }

    // ---- Settings の導出 (Python の Settings の property) ----

    fn stage(&self) -> &str {
        self.s.stage.as_str()
    }

    fn region(&self) -> &str {
        &self.s.general.region
    }

    fn fmt(&self, template: &str) -> String {
        pyformat(template, &format_vars(self.s))
    }

    fn resource_prefix(&self) -> String {
        self.fmt(&self.s.general.prefix_template)
    }

    fn external_role_prefix(&self) -> String {
        let namespace = self
            .s
            .iam
            .namespace
            .as_deref()
            .filter(|n| !n.is_empty())
            .unwrap_or(&self.s.general.namespace);
        pyformat(
            &self.s.general.prefix_template,
            &[
                ("namespace", namespace),
                ("stage", self.stage()),
                ("project", self.s.project_name()),
            ],
        )
    }

    /// `Settings.project_secrets_base` (store / pocket_key_format だけ)
    fn project_secrets_base(&self) -> Secrets {
        let base = self.s.container.values().find_map(|c| c.secrets.as_ref());
        let mut secrets = empty_secrets();
        if let Some(b) = base {
            secrets.store = b.store;
            secrets.pocket_key_format = b.pocket_key_format.clone();
        }
        secrets
    }

    /// `_permissions_boundary` (dsql / backup のサービスロール共通)
    fn service_permissions_boundary(&self) -> Option<String> {
        if let Some(env) = self.env_nonempty("POCKET_PERMISSIONS_BOUNDARY_ARN") {
            return Some(env);
        }
        let mut names: Vec<&String> = self.s.container.keys().collect();
        names.sort();
        names.into_iter().find_map(|name| {
            self.s
                .container
                .get(name)?
                .permissions_boundary
                .clone()
                .filter(|b| !b.is_empty())
        })
    }

    // ---- 外部入力 ----

    /// `resolve_hosted_zone_id` (override 優先 → domain から検索 → None)
    fn hosted_zone_id(
        &self,
        domain: Option<&str>,
        override_id: Option<&str>,
    ) -> Result<Option<String>, ContextError> {
        if let Some(o) = override_id.filter(|o| !o.is_empty()) {
            return Ok(Some(o.to_string()));
        }
        let Some(domain) = domain.filter(|d| !d.is_empty()) else {
            return Ok(None);
        };
        if let Some(id) = self.zone_ids.borrow().get(domain) {
            return Ok(Some(id.clone()));
        }
        self.log(
            MessageLevel::Log,
            format!("Searching hostedzone_id from domain: {domain}"),
        );
        if self.zones.borrow().is_none() {
            self.log(MessageLevel::Log, "Requesting Route53 hosted zone list...");
            let zones = self.inputs.external.hosted_zones().map_err(ContextError)?;
            *self.zones.borrow_mut() = Some(zones);
        }
        let zones = self.zones.borrow();
        let zones = zones.as_ref().expect("取得済み");
        let mut matched: Vec<&HostedZone> = zones
            .iter()
            .filter(|z| {
                let name = z.name.trim_matches('.');
                domain == name || domain.ends_with(&format!(".{name}"))
            })
            .collect();
        if matched.is_empty() {
            return Err(ContextError::new(format!(
                "No route53 hosted zone for the domain. [{domain}]\n\
                 Check your route53 hosted zone or set hosted_zone_id in pocket.toml"
            )));
        }
        // sorted(key=len(Name), reverse=True)[0] (同じ長さなら先のもの)
        matched.sort_by_key(|z| std::cmp::Reverse(z.name.chars().count()));
        let best = matched[0];
        let id = best
            .id
            .get("/hostedzone/".len()..)
            .unwrap_or_default()
            .to_string();
        self.log(MessageLevel::Log, "Found hostedzone");
        self.log(MessageLevel::Log, format!("  Name: {}", best.name));
        self.log(MessageLevel::Log, format!("  Id: {id}"));
        self.zone_ids
            .borrow_mut()
            .insert(domain.to_string(), id.clone());
        Ok(Some(id))
    }

    /// `_get_deploy_hash`
    fn deploy_hash(&self) -> String {
        if let Some(h) = self.env_nonempty("DEPLOY_HASH") {
            return h;
        }
        if self.env_nonempty("AWS_LAMBDA_FUNCTION_NAME").is_some() {
            return "unknown".to_string();
        }
        self.inputs
            .external
            .git_short_hash()
            .unwrap_or_else(|| "unknown".to_string())
    }

    // ---- secrets ----

    /// `_injected_managed_specs`
    fn injected_managed_specs(&self) -> OrderedMap<ManagedSecretSpec> {
        let mut injected = OrderedMap::default();
        if self.s.cloudfront.values().any(|cf| cf.enable_origin_verify) {
            injected.insert(
                ORIGIN_VERIFY_SECRET_KEY,
                managed_spec(ManagedSecretType::OriginVerifySecret),
            );
        }
        let mut keys: Vec<&String> = self
            .s
            .cloudfront
            .values()
            .filter_map(|cf| cf.waf.as_ref())
            .flat_map(|waf| waf.allow_rules.iter())
            .filter_map(|r| r.header.as_ref().filter(|h| !h.is_empty()))
            .collect();
        keys.sort();
        keys.dedup();
        for key in keys {
            injected.insert(key.clone(), managed_spec(ManagedSecretType::WafAllowSecret));
        }
        injected
    }

    /// `SecretsContext.from_settings`
    fn secrets_context(
        &self,
        secrets: &Secrets,
        pocket_key: Option<String>,
    ) -> Result<SecretsContext, ContextError> {
        let pocket_key = pocket_key.unwrap_or_else(|| self.fmt(secrets.pocket_key_format.as_str()));
        let mut user = OrderedMap::default();
        for (key, spec) in secrets.user.iter() {
            let name = match (&spec.name, spec.r#type) {
                (Some(name), _) => self.fmt(name),
                (None, Some(t)) => {
                    let store = spec.store.unwrap_or(secrets.store);
                    user_secret_path(&pocket_key, user_type_str(t), store)
                }
                (None, None) => {
                    return Err(ContextError::new(
                        "user secret spec has neither name nor type",
                    ))
                }
            };
            user.insert(
                key.clone(),
                UserSecretSpec {
                    name: Some(name),
                    ..spec.clone()
                },
            );
        }
        let mut sc = SecretsContext {
            store: secrets.store,
            managed: secrets.managed.clone(),
            user,
            extra_resources: secrets.extra_resources.clone(),
            require_list_secrets: secrets.require_list_secrets,
            region: self.region().to_string(),
            pocket_key,
            stage: self.stage().to_string(),
            project_name: self.s.project_name().to_string(),
            allowed_sm_resources: Vec::new(),
            allowed_ssm_resources: Vec::new(),
        };
        // check_entry
        let has_resources = sc.user.values().any(|s| s.name.is_some())
            || !sc.managed.is_empty()
            || !sc.extra_resources.is_empty()
            || sc.require_list_secrets;
        if !has_resources {
            self.log(
                MessageLevel::Log,
                "No secret resouces are associated to lambda.The data is for reference only.",
            );
        }
        sc.allowed_sm_resources = self.allowed_sm_resources(&sc)?;
        sc.allowed_ssm_resources = allowed_ssm_resources(&sc);
        Ok(sc)
    }

    /// `SecretsContext.allowed_sm_resources`
    fn allowed_sm_resources(&self, sc: &SecretsContext) -> Result<Vec<String>, ContextError> {
        let mut resources: Vec<String> = Vec::new();
        for spec in sc.user.values() {
            if spec.store.unwrap_or(sc.store) == StoreType::Sm {
                if let Some(name) = &spec.name {
                    resources.push(name.clone());
                }
            }
        }
        if !sc.managed.is_empty() && sc.store == StoreType::Sm {
            self.log(
                MessageLevel::Log,
                format!("Requesting pocket secrets {} ...", sc.pocket_key),
            );
            match self
                .inputs
                .external
                .managed_secret_arn(&sc.pocket_key, &sc.region)
                .map_err(ContextError)?
            {
                Some(arn) => resources.push(arn),
                None => self.log(
                    MessageLevel::Warning,
                    "Pocket managed secrets is not ready. The context is not complete data.\n\
                     Use deploy command or create secrets before create awslambda.",
                ),
            }
        }
        resources.extend(
            sc.extra_resources
                .iter()
                .filter(|r| r.contains(":secretsmanager:") || !r.starts_with("arn:"))
                .cloned(),
        );
        Ok(resources
            .into_iter()
            .filter(|r| !r.is_empty())
            .map(|r| {
                if r.starts_with("arn:") {
                    r
                } else {
                    format!("arn:aws:secretsmanager:${{AWS::Region}}:${{AWS::AccountId}}:secret:{r}-??????")
                }
            })
            .collect())
    }

    /// `_container_secrets_context`
    fn container_secrets(
        &self,
        name: &str,
        c: &Container,
    ) -> Result<Option<SecretsContext>, ContextError> {
        let Some(secrets) = &c.secrets else {
            return Ok(None);
        };
        let own_managed = OrderedMap(
            secrets
                .managed
                .iter()
                .filter(|(_, spec)| !spec.shared)
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect(),
        );
        if own_managed.is_empty()
            && secrets.extra_resources.is_empty()
            && !secrets.require_list_secrets
        {
            return Ok(None);
        }
        let view = Secrets {
            managed: own_managed,
            user: OrderedMap::default(),
            ..secrets.clone()
        };
        let namespace = format!("{name}-{}", self.s.general.namespace);
        let pocket_key = pyformat(
            secrets.pocket_key_format.as_str(),
            &[
                ("namespace", namespace.as_str()),
                ("stage", self.stage()),
                ("project", self.s.project_name()),
            ],
        );
        self.secrets_context(&view, Some(pocket_key)).map(Some)
    }

    /// `_container_shared_secrets_context`
    fn container_shared_secrets(
        &self,
        c: &Container,
    ) -> Result<Option<SecretsContext>, ContextError> {
        let injected = self.injected_managed_specs();
        if c.secrets.is_none() && injected.is_empty() {
            return Ok(None);
        }
        let base = c
            .secrets
            .clone()
            .unwrap_or_else(|| self.project_secrets_base());
        let mut managed = OrderedMap(
            base.managed
                .iter()
                .filter(|(_, spec)| spec.shared)
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect(),
        );
        for (k, v) in injected.iter() {
            managed.insert(k.clone(), v.clone());
        }
        let view = Secrets {
            managed,
            extra_resources: Vec::new(),
            require_list_secrets: false,
            ..base
        };
        if view.managed.is_empty() && view.user.is_empty() {
            return Ok(None);
        }
        self.secrets_context(&view, None).map(Some)
    }

    /// `_project_secrets_context`
    fn project_secrets(&self) -> Result<Option<SecretsContext>, ContextError> {
        let injected = self.injected_managed_specs();
        let declared: Vec<&Secrets> = self
            .s
            .container
            .values()
            .filter_map(|c| c.secrets.as_ref())
            .collect();
        if declared.is_empty() && injected.is_empty() {
            return Ok(None);
        }
        let mut managed: OrderedMap<ManagedSecretSpec> = OrderedMap::default();
        let mut user: OrderedMap<UserSecretSpec> = OrderedMap::default();
        for s in &declared {
            for (key, spec) in s.managed.iter() {
                if spec.shared && !managed.contains_key(key) {
                    managed.insert(key.clone(), spec.clone());
                }
            }
            for (key, spec) in s.user.iter() {
                if !user.contains_key(key) {
                    user.insert(key.clone(), spec.clone());
                }
            }
        }
        for (k, v) in injected.iter() {
            managed.insert(k.clone(), v.clone());
        }
        let view = Secrets {
            managed,
            user,
            ..self.project_secrets_base()
        };
        self.secrets_context(&view, None).map(Some)
    }

    // ---- container ----

    fn api_gateway(
        &self,
        apigw: &crate::model::ApiGateway,
    ) -> Result<ApiGatewayContext, ContextError> {
        let domain = if apigw.create_records {
            apigw.domain.as_deref()
        } else {
            None
        };
        Ok(ApiGatewayContext {
            domain: apigw.domain.clone(),
            create_records: apigw.create_records,
            hosted_zone_id_override: apigw.hosted_zone_id_override.clone(),
            disable_execute_api_endpoint: apigw.domain.as_deref().is_some_and(|d| !d.is_empty()),
            hosted_zone_id: self
                .hosted_zone_id(domain, apigw.hosted_zone_id_override.as_deref())?,
        })
    }

    fn handler(
        &self,
        handler: &LambdaHandler,
        key: &str,
        container: &str,
        resource_prefix: &str,
    ) -> Result<LambdaHandlerContext, ContextError> {
        let apigateway = handler
            .apigateway
            .as_ref()
            .map(|a| self.api_gateway(a))
            .transpose()?;
        let sqs = handler.sqs.as_ref().map(|sqs| {
            let name = format!("{resource_prefix}{container}-{key}");
            let dead_letter_alert = sqs
                .dead_letter_alert
                .as_ref()
                .filter(|a| a.enabled)
                .map(|a| SqsDeadLetterAlertContext {
                    email: a.email.clone(),
                    eventbridge: a.eventbridge,
                    topic_name: format!("{name}-dead-letter-alert"),
                    alarm_name: format!("{name}-dead-letter-alert"),
                });
            SqsContext {
                retain: false,
                batch_size: sqs.batch_size,
                message_retention_period: sqs.message_retention_period,
                maximum_concurrency: sqs.maximum_concurrency,
                dead_letter_max_receive_count: sqs.dead_letter_max_receive_count,
                dead_letter_message_retention_period: sqs.dead_letter_message_retention_period,
                report_batch_item_failures: sqs.report_batch_item_failures,
                name,
                visibility_timeout: handler.timeout * 6,
                dead_letter_alert,
            }
        });
        let function_name = format!("{resource_prefix}{container}-{key}");
        Ok(LambdaHandlerContext {
            command: handler.command.clone(),
            timeout: handler.timeout,
            memory_size: handler.memory_size,
            reserved_concurrency: handler.reserved_concurrency,
            envs: handler.envs.clone(),
            region: self.region().to_string(),
            apigateway,
            sqs,
            key: key.to_string(),
            log_group_name: format!("/aws/lambda/{function_name}"),
            function_name,
            export_api_domain: None,
            cloudformation_cert_ref_name: format!("{}Certificate", capitalize(key)),
        })
    }

    /// `ContainerContext.from_settings`
    fn container(&self, name: &str, c: &Container) -> Result<ContainerContext, ContextError> {
        let resource_prefix = self.resource_prefix();
        let vpc = c
            .vpc
            .as_ref()
            .map(|v| VpcContext::from_settings(v, &self.s.general));
        let secrets = self.container_secrets(name, c)?;
        let shared_secrets = self.container_shared_secrets(c)?;
        let mut handlers = OrderedMap::default();
        let mut use_route53 = false;
        let mut use_sqs = false;
        for (key, handler) in c.handlers.iter() {
            handlers.insert(
                key.clone(),
                self.handler(handler, key, name, &resource_prefix)?,
            );
            use_route53 |= handler.apigateway.is_some();
            use_sqs |= handler.sqs.is_some();
        }
        let efs = c.vpc.as_ref().and_then(|v| v.efs.as_ref());
        let django = c
            .django
            .as_ref()
            .map(|d| DjangoContext::from_settings(d, Some((self.s, c))))
            .transpose()?;
        let mut ctx = ContainerContext {
            name: name.to_string(),
            vpc,
            secrets,
            shared_secrets,
            dockerfile_path: c.dockerfile_path.clone(),
            envs: c.envs.clone(),
            signing_key_imports: OrderedMap::default(),
            inbound: OrderedMap::default(),
            platform: c.platform,
            django,
            region: self.region().to_string(),
            slug: self.s.slug(),
            stage: self.stage().to_string(),
            namespace: self.s.general.namespace.clone(),
            handlers,
            ecr_name: c
                .ecr_name
                .clone()
                .unwrap_or_else(|| format!("{resource_prefix}{name}-lambda")),
            resource_prefix,
            ecr_name_overridden: c.ecr_name.is_some(),
            promote_commit_hash: None,
            use_s3: self.s.s3.is_some(),
            use_ses: self.s.ses.is_some(),
            use_route53,
            use_sqs,
            use_efs: efs.is_some(),
            permissions_boundary: self
                .env_nonempty("POCKET_PERMISSIONS_BOUNDARY_ARN")
                .or_else(|| c.permissions_boundary.clone().filter(|b| !b.is_empty()))
                .or_else(|| c.permissions_boundary.clone()),
            iam: ContainerIamContext {
                managed_policy_arns: c.iam.managed_policy_arns.clone(),
                inline_policies: c.iam.inline_policies.clone(),
            },
            external_roles: self.s.iam.external_roles,
            external_role_prefix: self.external_role_prefix(),
            efs_local_mount_path: efs
                .map(|e| e.local_mount_path.0.clone())
                .unwrap_or_default(),
            build: BuildContext {
                backend: c.build.backend,
                compute_type: c.build.compute_type.clone(),
                depot_project_id: c.build.depot_project_id.clone(),
            },
            lambda_architecture: String::new(),
            allowed_sm_resources: Vec::new(),
            allowed_ssm_resources: Vec::new(),
            require_list_secrets: false,
            inbound_runtime_config: OrderedMap::default(),
        };
        ctx.refresh_computed();
        Ok(ctx)
    }

    // ---- 外部サービス・DB ----

    fn neon(&self, neon: &crate::model::Neon) -> NeonContext {
        NeonContext {
            pg_version: neon.pg_version,
            project_name: neon.project_name.clone(),
            branch_name: self.fmt(
                neon.branch_name
                    .as_ref()
                    .map(FormatStr::as_str)
                    .filter(|b| !b.is_empty())
                    .unwrap_or("main"),
            ),
            branch_name_specified: neon.branch_name.is_some(),
            stage: Some(self.stage().to_string()),
            parent_branch_name: neon
                .parent_branch_name
                .as_ref()
                .filter(|p| !p.as_str().is_empty())
                .map(|p| self.fmt(p.as_str())),
            name: self.s.project_name().to_string(),
            role_name: self.s.project_name().to_string(),
        }
    }

    fn backup_vault_name(&self) -> String {
        format!("{}backup", self.resource_prefix())
    }

    fn backup_role_name(&self) -> String {
        if self.s.iam.external_roles {
            format!("{}backup-role", self.external_role_prefix())
        } else {
            format!("{}backup-role", self.resource_prefix())
        }
    }

    fn dsql_backup_plan(&self, plan: &crate::model::DsqlBackupPlan) -> BackupPlanContext {
        let rules = [
            (
                "daily",
                &plan.daily.cron,
                plan.daily.delete_after_days,
                plan.daily.cold_storage_after_days,
            ),
            (
                "weekly",
                &plan.weekly.cron,
                plan.weekly.delete_after_days,
                plan.weekly.cold_storage_after_days,
            ),
            (
                "monthly",
                &plan.monthly.cron,
                plan.monthly.delete_after_days,
                plan.monthly.cold_storage_after_days,
            ),
        ];
        self.backup_plan("dsql", &rules)
    }

    fn rds_backup_plan(&self, plan: &crate::model::RdsBackupPlan) -> BackupPlanContext {
        let rules = [
            (
                "weekly",
                &plan.weekly.cron,
                plan.weekly.delete_after_days,
                0,
            ),
            (
                "monthly",
                &plan.monthly.cron,
                plan.monthly.delete_after_days,
                0,
            ),
        ];
        self.backup_plan("rds", &rules)
    }

    fn backup_plan(&self, service: &str, rules: &[(&str, &String, i64, i64)]) -> BackupPlanContext {
        BackupPlanContext {
            service: service.to_string(),
            plan_name: format!("{}backup-{service}", self.resource_prefix()),
            rules: rules
                .iter()
                .map(|(name, cron, delete, cold)| BackupRuleContext {
                    name: name.to_string(),
                    schedule_expression: format!("cron({cron})"),
                    delete_after_days: *delete,
                    cold_storage_after_days: *cold,
                })
                .collect(),
        }
    }

    fn backup(&self, backup: Option<&Backup>) -> BackupContext {
        let mut plans = Vec::new();
        if let Some(plan) = backup.and_then(|b| b.dsql.as_ref()) {
            plans.push(self.dsql_backup_plan(plan));
        }
        if let Some(plan) = backup.and_then(|b| b.rds.as_ref()) {
            plans.push(self.rds_backup_plan(plan));
        }
        let prefix = self.resource_prefix();
        let mut cleanup = Vec::new();
        let mut legacy = Vec::new();
        if self.s.dsql.is_some() {
            cleanup.push(format!("{prefix}backup-dsql"));
            legacy.push(format!("{prefix}dsql-backup"));
        }
        if self.s.rds.as_ref().is_some_and(|r| r.managed) {
            cleanup.push(format!("{prefix}backup-rds"));
        }
        cleanup.extend(legacy.iter().cloned());
        BackupContext {
            region: self.region().to_string(),
            vault_name: self.backup_vault_name(),
            role_name: self.backup_role_name(),
            deletable: backup.is_some_and(|b| b.deletable),
            timezone: backup.map_or_else(|| "UTC".to_string(), |b| b.timezone.clone()),
            plans,
            cleanup_plan_names: cleanup,
            legacy_plan_names: legacy,
            permissions_boundary: self.service_permissions_boundary(),
            external_roles: self.s.iam.external_roles,
        }
    }

    fn dsql(&self, dsql: &crate::model::Dsql) -> DsqlContext {
        let secrets = self.project_secrets_base();
        let pocket_key = self.fmt(secrets.pocket_key_format.as_str());
        DsqlContext {
            region: self.region().to_string(),
            tag_name: format!("{}dsql", self.resource_prefix()),
            backup_vault_name: self.backup_vault_name(),
            backup_role_name: self.backup_role_name(),
            deletion_protection: dsql.deletion_protection,
            backup: self
                .s
                .backup
                .as_ref()
                .and_then(|b| b.dsql.as_ref())
                .map(|plan| self.dsql_backup_plan(plan)),
            endpoint_secret_name: user_secret_path(&pocket_key, DSQL_ENDPOINT, secrets.store),
            endpoint_secret_store: secrets.store,
            permissions_boundary: self.service_permissions_boundary(),
            external_roles: self.s.iam.external_roles,
        }
    }

    fn rds(&self, rds: &crate::model::Rds) -> Result<RdsContext, ContextError> {
        if !rds.managed {
            return Ok(RdsContext::unmanaged(
                self.region().to_string(),
                rds.secret_arn.clone(),
                rds.security_group_id.clone(),
            ));
        }
        let Some(vpc) = &rds.vpc else {
            return Err(ContextError::new("rds.vpc must be resolved by resolve_vpc"));
        };
        let prefix = self.resource_prefix();
        let database_name = match rds.database.as_deref().filter(|d| !d.is_empty()) {
            Some(d) => d.to_string(),
            None => format!("{}_{}", self.stage(), self.s.project_name()).replace('-', "_"),
        };
        Ok(RdsContext {
            managed: true,
            vpc: Some(VpcContext::from_settings(vpc, &self.s.general)),
            min_capacity: rds.min_capacity,
            max_capacity: rds.max_capacity,
            snapshot_identifier: rds.snapshot_identifier.clone(),
            backup_retention_days: rds.backup.retention_days,
            region: self.region().to_string(),
            cluster_identifier: format!("{prefix}aurora"),
            instance_identifier: format!("{prefix}aurora-1"),
            database_name,
            master_username: "postgres".to_string(),
            subnet_group_name: format!("{prefix}aurora"),
            security_group_name: format!("{prefix}aurora-rds"),
            slug: self.s.slug(),
            password_strategy: rds.password_strategy,
            rotation_schedule: rds.rotation_schedule.clone(),
            rotation_duration: rds.rotation_duration.clone(),
            credentials_secret_name: format!("{prefix}aurora-credentials"),
            secret_store: self.project_secrets_base().store,
            secret_arn: None,
            security_group_id: None,
        })
    }

    fn ses(&self, ses: &crate::model::Ses) -> SesContext {
        SesContext {
            from_email: ses.from_email.clone(),
            region: ses
                .region
                .clone()
                .filter(|r| !r.is_empty())
                .unwrap_or_else(|| self.region().to_string()),
            configuration_set: ses.configuration_set.clone(),
        }
    }

    fn s3(&self, s3: &crate::model::S3) -> S3Context {
        S3Context {
            region: self.region().to_string(),
            bucket_name: self.fmt(s3.bucket_name_format.as_str()),
            cors: s3.cors.as_ref().map(|cors| S3CorsContext {
                methods: cors.methods.clone(),
                cloudfront_names: match &cors.cloudfront {
                    OneOrMany::One(name) => vec![name.clone()],
                    OneOrMany::Many(names) => names.clone(),
                },
            }),
            versioning: s3.versioning,
            lifecycle_rules: s3
                .lifecycle_rules
                .iter()
                .map(|r| S3LifecycleRuleContext {
                    id: r.id.clone(),
                    prefix: r.prefix.clone(),
                    expiration_days: r.expiration_days,
                    noncurrent_version_expiration_days: r.noncurrent_version_expiration_days,
                })
                .collect(),
        }
    }

    // ---- cloudfront ----

    fn route(&self, route: &Route, cloudfront_name: &str) -> RouteContext {
        let origin_path = route.origin_path.clone().unwrap_or_default();
        let versioning = route.versioning;
        let delete_stale = Some(route.delete_stale.unwrap_or(!matches!(
            versioning,
            Some(Versioning::ContentHash) | Some(Versioning::ObjectHash)
        )));
        let name = crate::model::cloudfront::route_logical_name(&route.path_pattern);
        let key_prefix = {
            let joined = format!("{origin_path}{}", rstrip_slash_star(&route.path_pattern));
            let prefix = joined.trim_start_matches('/');
            if prefix.is_empty() {
                String::new()
            } else {
                format!("{prefix}/")
            }
        };
        RouteContext {
            r#type: route.r#type,
            handler: route.handler.clone(),
            path_pattern: route.path_pattern.clone(),
            is_default: route.is_default,
            is_spa: route.is_spa,
            versioning,
            spa_fallback_html: route.spa_fallback_html.clone(),
            versioned_max_age: route.versioned_max_age,
            r#ref: route.r#ref.clone(),
            signed: route.signed,
            build_cmd: route.build.as_ref().map(|b| b.cmd.clone()),
            upload_dir: match &route.build {
                Some(b) => Some(b.dir.clone()),
                None => route.upload_dir.clone(),
            },
            delete_stale,
            origin_path,
            require_token: route.require_token,
            login_path: route.login_path.clone(),
            cloudfront_name: cloudfront_name.to_string(),
            sources: route.sources.clone(),
            map: route.map.clone(),
            is_lambda: route.r#type == RouteType::Lambda,
            is_content_hash: versioning == Some(Versioning::ContentHash),
            is_deploy_hash: versioning == Some(Versioning::DeployHash),
            is_object_hash: versioning == Some(Versioning::ObjectHash),
            key_prefix,
            url_prefix: format!("{}/", rstrip_slash_star(&route.path_pattern)),
            static_meta_prefix: format!(".pocket/static/{cloudfront_name}/{name}/"),
            map_path: match route.map.as_deref().filter(|m| !m.is_empty()) {
                Some(m) => m.to_string(),
                None => format!("pocket_static/{cloudfront_name}-{name}.map"),
            },
            yaml_key: name.split('-').map(capitalize).collect(),
            name,
        }
    }

    fn cloudfront(&self, cf: &CloudFront, name: &str) -> Result<CloudFrontContext, ContextError> {
        let Some(s3) = &self.s.s3 else {
            return Err(ContextError::new(
                "s3 is required when cloudfront is configured",
            ));
        };
        let routes: Vec<RouteContext> = cf.routes.iter().map(|r| self.route(r, name)).collect();
        let deploy_hash = if routes.iter().any(|r| r.is_deploy_hash) {
            self.deploy_hash()
        } else {
            String::new()
        };
        let redirect_from = cf
            .redirect_from
            .iter()
            .map(|rf| {
                Ok(RedirectFromContext {
                    domain: rf.domain.clone(),
                    hosted_zone_id_override: rf.hosted_zone_id_override.clone(),
                    yaml_key: camel_logical_name(&rf.domain),
                    hosted_zone_id: self
                        .hosted_zone_id(Some(&rf.domain), rf.hosted_zone_id_override.as_deref())?,
                })
            })
            .collect::<Result<Vec<_>, ContextError>>()?;
        let waf = cf.waf.as_ref().map(|waf| {
            let allow_rules: Vec<CloudFrontWafAllowRuleContext> = waf
                .allow_rules
                .iter()
                .map(|r| {
                    let (constraint, search) = waf_path_match(r.path.as_deref());
                    CloudFrontWafAllowRuleContext {
                        path: r.path.clone(),
                        header: r.header.clone(),
                        path_positional_constraint: constraint,
                        path_search_string: search,
                    }
                })
                .collect();
            let mut header_secret_keys: Vec<String> = allow_rules
                .iter()
                .filter_map(|r| r.header.clone().filter(|h| !h.is_empty()))
                .collect();
            header_secret_keys.sort();
            header_secret_keys.dedup();
            CloudFrontWafContext {
                enable_ip_set: waf.enable_ip_set,
                managed_rule_groups: waf.managed_rule_groups.clone(),
                allow_rules,
                rate_limits: waf
                    .rate_limits
                    .iter()
                    .map(|r| {
                        let (constraint, search) = waf_path_match(r.path.as_deref());
                        CloudFrontWafRateLimitContext {
                            path: r.path.clone(),
                            limit: r.limit,
                            window_sec: r.window_sec,
                            action: r.action,
                            path_positional_constraint: constraint,
                            path_search_string: search,
                        }
                    })
                    .collect(),
                header_secret_keys,
            }
        });
        let managed_assets = cf.managed_assets.clone();
        let default_route = routes
            .iter()
            .find(|r| r.is_default)
            .cloned()
            .ok_or_else(|| ContextError::new("default route should be defined"))?;
        let slug = format!("{}-{name}", self.s.slug());
        Ok(CloudFrontContext {
            name: name.to_string(),
            region: self.region().to_string(),
            s3_region: self.region().to_string(),
            stage: self.stage().to_string(),
            domain: cf.domain.clone(),
            hosted_zone_id_override: cf.hosted_zone_id_override.clone(),
            yaml_key: slug.split('-').map(capitalize).collect(),
            slug,
            bucket_name: self.fmt(s3.bucket_name_format.as_str()),
            resource_prefix: self.resource_prefix(),
            has_redirect_from: !redirect_from.is_empty(),
            redirect_from,
            bucket_policy_prefix: bucket_policy_prefix(
                &routes,
                managed_assets.as_deref().is_some_and(|m| !m.is_empty()),
            ),
            default_route,
            uploadable_routes: routes
                .iter()
                .filter(|r| r.upload_dir.as_deref().is_some_and(|d| !d.is_empty()))
                .cloned()
                .collect(),
            extra_s3_routes: routes
                .iter()
                .filter(|r| !r.is_default && !r.is_lambda)
                .cloned()
                .collect(),
            extra_lambda_routes: routes
                .iter()
                .filter(|r| r.is_lambda && !r.is_default)
                .cloned()
                .collect(),
            has_lambda_route: routes.iter().any(|r| r.is_lambda),
            managed_asset_files: self.managed_asset_files(managed_assets.as_deref()),
            routes,
            signing_key: cf.signing_key.clone(),
            token_secret: cf.token_secret.clone(),
            basic_auth: cf.basic_auth.clone(),
            api_origins: OrderedMap::default(),
            managed_assets,
            deploy_hash,
            waf,
            enable_origin_verify: cf.enable_origin_verify,
            hosted_zone_id: self
                .hosted_zone_id(cf.domain.as_deref(), cf.hosted_zone_id_override.as_deref())?,
        })
    }

    /// `CloudFrontContext.managed_asset_files`
    fn managed_asset_files(&self, managed_assets: Option<&str>) -> Vec<String> {
        let Some(base) = managed_assets.filter(|m| !m.is_empty()) else {
            return Vec::new();
        };
        let base = base.trim_end_matches('/');
        let external = self.inputs.external;
        external
            .dir_files(&format!("{base}/{}", self.stage()))
            .or_else(|| external.dir_files(&format!("{base}/default")))
            .unwrap_or_default()
    }

    // ---- 組み立て ----

    /// `Context._build_api_origins` / `_apply_cloudfront_cross_refs`
    fn apply_cloudfront_cross_refs(
        &self,
        containers: &mut OrderedMap<ContainerContext>,
        cloudfronts: &mut OrderedMap<CloudFrontContext>,
    ) -> Result<(), ContextError> {
        let slug = self.s.slug();
        for (c_name, c) in containers.iter_mut() {
            for (h_key, h) in c.handlers.iter_mut() {
                if h.apigateway.is_some() && h.export_api_domain.as_deref().unwrap_or("").is_empty()
                {
                    h.export_api_domain = Some(format!("{slug}-{c_name}-{h_key}-api-domain"));
                }
            }
        }
        for (_, cf) in cloudfronts.iter_mut() {
            let mut api_origins = OrderedMap::default();
            for route in &cf.routes {
                let Some(handler) = route.handler.as_deref().filter(|h| !h.is_empty()) else {
                    continue;
                };
                if !route.is_lambda {
                    continue;
                }
                let (c_name, h_key) = parse_handler_ref(handler)?;
                api_origins.insert(handler, format!("{slug}-{c_name}-{h_key}-api-domain"));
            }
            if !api_origins.is_empty() {
                cf.api_origins = api_origins;
            }
        }
        // signing_key_imports: (secret key, env, export)
        let mut signing: Vec<(String, String, String)> = Vec::new();
        for cf in cloudfronts.values() {
            let Some(key) = cf.signing_key.as_deref().filter(|k| !k.is_empty()) else {
                continue;
            };
            let Some(spec) = self.s.find_managed_secret(key) else {
                continue;
            };
            let suffix = match spec.options.get("id_environ_suffix") {
                Some(StrOrInt::Str(s)) => s.clone(),
                Some(StrOrInt::Int(i)) => i.to_string(),
                None => "_ID".to_string(),
            };
            signing.push((
                key.to_string(),
                format!("{key}{suffix}"),
                format!("{}-public-key-id", cf.slug),
            ));
        }
        if !signing.is_empty() {
            for (name, c) in self.s.container.iter() {
                let declared: Vec<&String> = c
                    .secrets
                    .as_ref()
                    .map(|s| s.managed.keys().collect())
                    .unwrap_or_default();
                let mut imports = OrderedMap::default();
                for (secret_key, env, export) in &signing {
                    if declared.contains(&secret_key) {
                        imports.insert(env.clone(), export.clone());
                    }
                }
                if !imports.is_empty() {
                    containers
                        .get_mut(name)
                        .expect("container は settings と同じ")
                        .signing_key_imports = imports;
                }
            }
        }
        Ok(())
    }

    /// `Context._inject_deploy_hash`
    fn inject_deploy_hash(
        &self,
        containers: &mut OrderedMap<ContainerContext>,
        cloudfronts: &OrderedMap<CloudFrontContext>,
    ) {
        let deploy_hash = cloudfronts
            .values()
            .map(|cf| cf.deploy_hash.clone())
            .find(|h| !h.is_empty())
            .unwrap_or_else(|| self.deploy_hash());
        for (_, c) in containers.iter_mut() {
            if !c.envs.contains_key("DEPLOY_HASH") {
                c.envs.insert("DEPLOY_HASH", deploy_hash.clone());
            }
        }
    }

    /// `Context._connect_inbound`
    fn connect_inbound(
        &self,
        containers: &mut OrderedMap<ContainerContext>,
    ) -> Result<OrderedMap<InboundContext>, ContextError> {
        let mut names: Vec<&String> = self.s.inbound.keys().collect();
        names.sort();
        let mut inbound = OrderedMap::default();
        for name in names {
            inbound.insert(name.clone(), InboundContext::from_settings(name, self.s)?);
        }
        for (name, inlet) in inbound.iter() {
            let (container, handler) = parse_handler_ref(&inlet.config.handler)?;
            let target = containers.get_mut(&container).ok_or_else(|| {
                ContextError::new(format!(
                    "inbound.{name}: container '{container}' がありません"
                ))
            })?;
            if target
                .permissions_boundary
                .as_deref()
                .unwrap_or("")
                .is_empty()
            {
                target.permissions_boundary =
                    self.env.get("FORGE_PERMISSIONS_BOUNDARY_ARN").cloned();
            }
            target.inbound.insert(name.clone(), inlet.clone());
            if let Some(sqs) = target
                .handlers
                .get_mut(&handler)
                .and_then(|h| h.sqs.as_mut())
            {
                sqs.message_retention_period = 1209600;
                sqs.dead_letter_message_retention_period = 1209600;
                sqs.retain = true;
            }
        }
        Ok(inbound)
    }

    /// `SchedulerContext.from_settings`
    fn scheduler(
        &self,
        scheduler: &crate::model::Scheduler,
        container_name: &str,
        container: &ContainerContext,
    ) -> Result<SchedulerContext, ContextError> {
        let resource_prefix = &container.resource_prefix;
        let mut entries = Vec::new();
        for (key, entry) in scheduler.schedules.iter() {
            let (c_name, h_key) = parse_handler_ref(&entry.handler)?;
            if c_name == container_name {
                entries.push((key, entry, h_key));
            }
        }
        let schedules: Vec<ScheduleEntryContext> = entries
            .iter()
            .map(|(key, entry, h_key)| {
                let payload = match entry.scheduler {
                    SchedulerKind::DjangoManagement => serde_json::json!({
                        "manage": entry.manage.clone().unwrap_or_default()
                    }),
                    SchedulerKind::Sqs => entry
                        .message
                        .as_ref()
                        .map_or_else(empty_object, |d| d.0.clone()),
                    SchedulerKind::Lambda => entry
                        .input
                        .as_ref()
                        .map_or_else(empty_object, |d| d.0.clone()),
                };
                let scheduler_name = serde_json::to_value(entry.scheduler)
                    .ok()
                    .and_then(|v| v.as_str().map(str::to_string))
                    .unwrap_or_default();
                ScheduleEntryContext {
                    key: key.to_string(),
                    is_django_management: entry.scheduler == SchedulerKind::DjangoManagement,
                    is_sqs: entry.scheduler == SchedulerKind::Sqs,
                    scheduler: scheduler_name,
                    handler: h_key.clone(),
                    schedule_expression: entry.schedule_expression(),
                    name: format!("{resource_prefix}{}", kebab(key)),
                    yaml_key: camel_logical_name(key),
                    input_json: crate::pyjson::dumps_unicode(&payload),
                }
            })
            .collect();
        let region = self.region();
        let mut invoked: Vec<&String> = entries
            .iter()
            .filter(|(_, e, _)| e.scheduler != SchedulerKind::Sqs)
            .map(|(_, _, h)| h)
            .collect();
        invoked.sort();
        invoked.dedup();
        let mut invoked_function_arns: Vec<String> = invoked
            .into_iter()
            .filter_map(|h| container.handlers.get(h))
            .map(|h| {
                format!(
                    "arn:aws:lambda:{region}:${{AWS::AccountId}}:function:{}",
                    h.function_name
                )
            })
            .collect();
        invoked_function_arns.sort();
        let mut sqs_handlers: Vec<&String> = entries
            .iter()
            .filter(|(_, e, _)| e.scheduler == SchedulerKind::Sqs)
            .map(|(_, _, h)| h)
            .collect();
        sqs_handlers.sort();
        sqs_handlers.dedup();
        let mut sqs_queue_arns: Vec<String> = sqs_handlers
            .into_iter()
            .filter_map(|h| container.handlers.get(h)?.sqs.as_ref())
            .map(|sqs| format!("arn:aws:sqs:{region}:${{AWS::AccountId}}:{}", sqs.name))
            .collect();
        sqs_queue_arns.sort();
        Ok(SchedulerContext {
            has_schedules: !schedules.is_empty(),
            schedules,
            role_name: if self.s.iam.external_roles {
                format!("{}scheduler-role", self.external_role_prefix())
            } else {
                format!("{resource_prefix}{container_name}-scheduler")
            },
            invoked_function_arns,
            sqs_queue_arns,
        })
    }

    /// `Context.check_django`
    fn check_django(&self, ctx: &Context) -> Result<(), ContextError> {
        for c in ctx.container.values() {
            let Some(django) = &c.django else { continue };
            for (key, storage) in django.storages.iter() {
                if storage.store == crate::model::StorageStore::S3 && ctx.s3.is_none() {
                    return Err(ContextError::new("s3 is required for s3 storage"));
                }
                if let Some(d) = storage.distribution.as_deref().filter(|d| !d.is_empty()) {
                    if !ctx.cloudfront.contains_key(d) {
                        return Err(ContextError::new(format!(
                            "storage '{key}': distribution '{d}' not found in cloudfront"
                        )));
                    }
                }
            }
            for cache in django.caches.values() {
                let has_efs = c.vpc.as_ref().is_some_and(|v| v.efs.is_some());
                if cache.store == crate::model::CacheStore::Efs && !has_efs {
                    return Err(ContextError::new("vpc is required for efs cache"));
                }
            }
        }
        Ok(())
    }

    /// `Context.from_settings`
    pub(crate) fn context(&self) -> Result<Context, ContextError> {
        let s = self.s;
        let general = GeneralContext::from_general_settings(&s.general)?;
        // _build_service_contexts
        let neon = s.neon.as_ref().map(|n| self.neon(n));
        let tidb = s.tidb.as_ref().map(|_| TiDbContext {});
        let dsql = s.dsql.as_ref().map(|d| self.dsql(d));
        let rds = s.rds.as_ref().map(|r| self.rds(r)).transpose()?;
        let ses = s.ses.as_ref().map(|x| self.ses(x));
        let s3 = s.s3.as_ref().map(|x| self.s3(x));
        let has_backup_target = s.dsql.is_some() || s.rds.as_ref().is_some_and(|r| r.managed);
        let backup = has_backup_target.then(|| self.backup(s.backup.as_ref()));

        let mut containers = OrderedMap::default();
        for (name, c) in s.container.iter() {
            containers.insert(name.clone(), self.container(name, c)?);
        }
        let inbound = self.connect_inbound(&mut containers)?;

        let mut cloudfronts = OrderedMap::default();
        for (name, cf) in s.cloudfront.iter() {
            cloudfronts.insert(name.clone(), self.cloudfront(cf, name)?);
        }
        if !containers.is_empty() {
            self.apply_cloudfront_cross_refs(&mut containers, &mut cloudfronts)?;
            self.inject_deploy_hash(&mut containers, &cloudfronts);
        }
        for (_, c) in containers.iter_mut() {
            c.refresh_computed();
        }

        let mut scheduler = OrderedMap::default();
        if let Some(sch) = s.scheduler.as_ref().filter(|x| !x.schedules.is_empty()) {
            if containers.is_empty() {
                return Err(ContextError::new(
                    "scheduler requires container (validated in settings)",
                ));
            }
            for (name, c) in containers.iter() {
                let sc = self.scheduler(sch, name, c)?;
                if sc.has_schedules {
                    scheduler.insert(name.clone(), sc);
                }
            }
        }

        let ctx = Context {
            general: Some(general),
            container: containers,
            secrets: self.project_secrets()?,
            neon,
            tidb,
            dsql,
            rds,
            backup,
            ses,
            inbound,
            inbound_domain: InboundDomainContext::from_settings(s),
            s3,
            cloudfront: cloudfronts,
            scheduler,
            external_roles: s.iam.external_roles,
            external_role_prefix: self.external_role_prefix(),
            project_name: s.project_name().to_string(),
            stage: self.stage().to_string(),
        };
        self.check_django(&ctx)?;
        Ok(ctx)
    }
}

/// `SecretsContext.allowed_ssm_resources`
fn allowed_ssm_resources(sc: &SecretsContext) -> Vec<String> {
    let mut resources: Vec<String> = Vec::new();
    for spec in sc.user.values() {
        if spec.store.unwrap_or(sc.store) == StoreType::Ssm {
            if let Some(name) = &spec.name {
                resources.push(name.clone());
            }
        }
    }
    if !sc.managed.is_empty() && sc.store == StoreType::Ssm {
        resources.push(format!(
            "arn:aws:ssm:${{AWS::Region}}:${{AWS::AccountId}}:parameter/{}/*",
            sc.pocket_key
        ));
    }
    resources.extend(
        sc.extra_resources
            .iter()
            .filter(|r| r.contains(":ssm:"))
            .cloned(),
    );
    resources
        .into_iter()
        .filter(|r| !r.is_empty())
        .map(|r| {
            if r.starts_with("arn:") {
                r
            } else {
                format!(
                    "arn:aws:ssm:${{AWS::Region}}:${{AWS::AccountId}}:parameter/{}",
                    r.trim_start_matches('/')
                )
            }
        })
        .collect()
}

fn empty_secrets() -> Secrets {
    Secrets {
        store: StoreType::Sm,
        pocket_key_format: FormatStr("{stage}-{project}-{namespace}".to_string()),
        managed: OrderedMap::default(),
        user: OrderedMap::default(),
        extra_resources: Vec::new(),
        require_list_secrets: false,
    }
}

fn managed_spec(t: ManagedSecretType) -> ManagedSecretSpec {
    ManagedSecretSpec {
        r#type: t,
        options: OrderedMap::default(),
        shared: false,
    }
}

fn user_type_str(t: crate::model::UserSecretType) -> &'static str {
    match t {
        crate::model::UserSecretType::NeonDatabaseUrl => "neon_database_url",
    }
}

fn empty_object() -> serde_json::Value {
    serde_json::Value::Object(serde_json::Map::new())
}

/// `_kebab`: `re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")`
fn kebab(key: &str) -> String {
    let lower = key.to_lowercase();
    let mut out = String::new();
    let mut in_sep = false;
    for c in lower.chars() {
        if c.is_ascii_lowercase() || c.is_ascii_digit() {
            out.push(c);
            in_sep = false;
        } else if !in_sep {
            out.push('-');
            in_sep = true;
        }
    }
    out.trim_matches('-').to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_kebab() {
        assert_eq!(kebab("Daily_Cleanup"), "daily-cleanup");
        assert_eq!(kebab("--a..b--"), "a-b");
    }
}
