"""0.29.0 multi-container ([container.<name>] dict 化) の挙動テスト。

- 旧 [awscontainer] の明示エラー (移行案内)
- container 名 / secrets 整合性の validator
- 命名の container slot 挿入 (function / ECR / stack / Export / queue)
- handler 参照のドット記法 (cloudfront routes / scheduler)
- per-container SecretsContext と project union (Context.secrets)
- runtime の container 解決 (POCKET_CONTAINER / 単一 fallback)
- 旧リソースの deploy 後掃除 (cleanup_legacy_container_resources)
"""

from __future__ import annotations

from unittest import mock

import pytest
from pocket_cli.resources.aws.cloudformation import ContainerStack

from pocket import settings
from pocket.context import Context
from pocket.runtime import resolve_container_name

_WSGI_CMD = "pocket.django.lambda_handlers.wsgi_handler"


def _base_data() -> dict:
    return {
        "stage": "dev",
        "general": {
            "region": "ap-northeast-1",
            "project_name": "testprj",
            "stages": ["dev"],
        },
    }


def _two_container_data() -> dict:
    data = _base_data()
    data["s3"] = {}
    data["container"] = {
        "mydjango": {
            "dockerfile_path": "Dockerfile",
            "handlers": {
                "wsgi": {"command": _WSGI_CMD, "apigateway": {}},
            },
            "secrets": {
                "managed": {"SECRET_KEY": {"type": "password"}},
            },
        },
        "v2": {
            "dockerfile_path": "v2/Dockerfile",
            "handlers": {
                "wsgi": {"command": "admin-v2", "apigateway": {}},
                "worker": {"command": "admin-v2", "sqs": {}},
            },
        },
    }
    data["cloudfront"] = {
        "web": {
            "routes": [
                {"path_pattern": "/v2/*", "type": "lambda", "handler": "v2.wsgi"},
                {"type": "lambda", "handler": "mydjango.wsgi", "is_default": True},
            ],
        },
    }
    return data


def test_legacy_awscontainer_rejected_with_migration_guide():
    data = _base_data()
    data["awscontainer"] = {"dockerfile_path": "Dockerfile"}
    with pytest.raises(ValueError, match=r"\[container\.<name>\]"):
        settings.Settings.model_validate(data)


def test_container_name_charset_restricted():
    data = _base_data()
    data["container"] = {"my-django": {"dockerfile_path": "Dockerfile"}}
    with pytest.raises(ValueError, match="container 名"):
        settings.Settings.model_validate(data)


def test_secrets_store_must_match_across_containers():
    data = _base_data()
    data["container"] = {
        "a": {"dockerfile_path": "Dockerfile", "secrets": {"store": "sm"}},
        "b": {"dockerfile_path": "Dockerfile", "secrets": {"store": "ssm"}},
    }
    with pytest.raises(ValueError, match="store は全 container で一致"):
        settings.Settings.model_validate(data)


def test_same_managed_key_without_shared_rejected():
    """同名 managed key の複数宣言は shared = true 必須 (偶然の同名を防ぐ)。"""
    data = _base_data()
    data["container"] = {
        "a": {
            "dockerfile_path": "Dockerfile",
            "secrets": {"managed": {"SECRET_KEY": {"type": "password"}}},
        },
        "b": {
            "dockerfile_path": "Dockerfile",
            "secrets": {"managed": {"SECRET_KEY": {"type": "password"}}},
        },
    }
    with pytest.raises(ValueError, match="shared = true"):
        settings.Settings.model_validate(data)


def test_shared_key_with_different_spec_rejected():
    data = _base_data()
    data["container"] = {
        "a": {
            "dockerfile_path": "Dockerfile",
            "secrets": {
                "managed": {"SECRET_KEY": {"type": "password", "shared": True}}
            },
        },
        "b": {
            "dockerfile_path": "Dockerfile",
            "secrets": {
                "managed": {
                    "SECRET_KEY": {
                        "type": "password",
                        "options": {"length": 32},
                        "shared": True,
                    }
                }
            },
        },
    }
    with pytest.raises(ValueError, match="異なる spec"):
        settings.Settings.model_validate(data)


def test_shared_key_lives_in_project_store():
    """shared = true の同名宣言は shared store (project パス) で値を共有する。"""
    data = _base_data()
    data["container"] = {
        "a": {
            "dockerfile_path": "Dockerfile",
            "secrets": {
                "managed": {"SECRET_KEY": {"type": "password", "shared": True}}
            },
        },
        "b": {
            "dockerfile_path": "Dockerfile",
            "secrets": {
                "managed": {"SECRET_KEY": {"type": "password", "shared": True}}
            },
        },
    }
    context = Context.from_settings(settings.Settings.model_validate(data))
    assert context.secrets is not None
    assert set(context.secrets.managed) == {"SECRET_KEY"}
    assert context.secrets.pocket_key == "dev-testprj-pocket"
    # container store 側には入らない (shared 宣言のみの container は view 無し)
    assert context.container["a"].secrets is None
    assert context.container["b"].secrets is None
    # 両 container の env には shared store view 経由で載る
    for name in ("a", "b"):
        shared_view = context.container[name].shared_secrets
        assert shared_view is not None
        assert "SECRET_KEY" in shared_view.managed
        assert shared_view.pocket_key == "dev-testprj-pocket"


def test_route_handler_requires_dot_notation():
    data = _two_container_data()
    data["cloudfront"]["web"]["routes"][0]["handler"] = "wsgi"
    with pytest.raises(ValueError, match="ドット記法"):
        settings.Settings.model_validate(data)


def test_route_handler_unknown_container_rejected():
    data = _two_container_data()
    data["cloudfront"]["web"]["routes"][0]["handler"] = "ghost.wsgi"
    with pytest.raises(ValueError, match="container 'ghost'"):
        settings.Settings.model_validate(data)


def test_multi_container_naming():
    """命名表どおりに container slot が挿入されること。"""
    context = Context.from_settings(
        settings.Settings.model_validate(_two_container_data())
    )
    django = context.container["mydjango"]
    v2 = context.container["v2"]
    assert django.handlers["wsgi"].function_name == "dev-testprj-pocket-mydjango-wsgi"
    assert v2.handlers["wsgi"].function_name == "dev-testprj-pocket-v2-wsgi"
    assert v2.handlers["worker"].sqs is not None
    assert v2.handlers["worker"].sqs.name == "dev-testprj-pocket-v2-worker"
    assert django.ecr_name == "dev-testprj-pocket-mydjango-lambda"
    assert v2.ecr_name == "dev-testprj-pocket-v2-lambda"
    # container stack 名
    assert ContainerStack(django).name == "dev-testprj-container-mydjango"
    assert ContainerStack(v2).name == "dev-testprj-container-v2"


def test_api_origins_use_container_qualified_export_names():
    context = Context.from_settings(
        settings.Settings.model_validate(_two_container_data())
    )
    cf = context.cloudfront["web"]
    assert cf.api_origins == {
        "v2.wsgi": "dev-testprj-v2-wsgi-api-domain",
        "mydjango.wsgi": "dev-testprj-mydjango-wsgi-api-domain",
    }
    assert (
        context.container["v2"].handlers["wsgi"].export_api_domain
        == "dev-testprj-v2-wsgi-api-domain"
    )


def test_unshared_secret_lives_in_container_store():
    """shared でない managed 宣言は container store に入る。"""
    context = Context.from_settings(
        settings.Settings.model_validate(_two_container_data())
    )
    django_sc = context.container["mydjango"].secrets
    assert django_sc is not None
    assert set(django_sc.managed) == {"SECRET_KEY"}
    # container store のパスに container 名が入る (SM コンソールでの識別性)
    assert django_sc.pocket_key == "dev-testprj-mydjango-pocket"
    # secrets 宣言の無い container には container store view が付かない
    assert context.container["v2"].secrets is None
    # shared store union には shared 宣言が無ければ managed は入らない
    assert context.secrets is not None
    assert context.secrets.managed == {}
    assert context.secrets.pocket_key == "dev-testprj-pocket"


def test_container_template_injects_pocket_container_env():
    context = Context.from_settings(
        settings.Settings.model_validate(_two_container_data())
    )
    yaml = ContainerStack(context.container["v2"]).yaml
    assert '"POCKET_CONTAINER": "v2"' in yaml
    # IAM Role 名にも container slot が入る (2 stack での衝突回避)
    assert "lambda-dev-testprj-v2-pocket" in yaml


def test_scheduler_entries_split_per_container():
    data = _two_container_data()
    data["scheduler"] = {
        "schedules": {
            "cleanup": {
                "scheduler": "pocket.sqs_scheduler",
                "rate": "15 minutes",
                "handler": "v2.worker",
                "message": {"job": "cleanup"},
            },
        }
    }
    context = Context.from_settings(settings.Settings.model_validate(data))
    # entry は v2 側の stack にだけ配置される
    assert set(context.scheduler) == {"v2"}
    sc = context.scheduler["v2"]
    assert sc.role_name == "dev-testprj-pocket-v2-scheduler"
    # template 内の論理参照用に handler はローカル key になる
    assert sc.schedules[0].handler == "worker"
    assert sc.sqs_queue_logical_names == ["WorkerSqsQueue"]


def test_resolve_container_name_env_and_fallback(monkeypatch):
    context = Context.from_settings(
        settings.Settings.model_validate(_two_container_data())
    )
    monkeypatch.delenv("POCKET_CONTAINER", raising=False)
    with pytest.raises(RuntimeError, match="POCKET_CONTAINER"):
        resolve_container_name(context)
    monkeypatch.setenv("POCKET_CONTAINER", "v2")
    assert resolve_container_name(context) == "v2"
    with pytest.raises(RuntimeError, match="'ghost'"):
        resolve_container_name(context, "ghost")

    # 単一 container なら省略時にそれが選択される
    monkeypatch.delenv("POCKET_CONTAINER", raising=False)
    data = _two_container_data()
    del data["container"]["v2"]
    data["cloudfront"]["web"]["routes"] = [
        {"type": "lambda", "handler": "mydjango.wsgi", "is_default": True}
    ]
    single = Context.from_settings(settings.Settings.model_validate(data))
    assert resolve_container_name(single) == "mydjango"


def test_cleanup_legacy_container_resources_deletes_old_stack_and_repo():
    from pocket_cli.cli import interaction
    from pocket_cli.cli.deploy_cli import cleanup_legacy_container_resources

    context = Context.from_settings(
        settings.Settings.model_validate(_two_container_data())
    )
    fake_cfn = mock.MagicMock()

    class _ClientError(Exception):
        pass

    fake_cfn.exceptions.ClientError = _ClientError
    fake_ecr = mock.MagicMock()

    class _NotFound(Exception):
        pass

    fake_ecr.exceptions.RepositoryNotFoundException = _NotFound

    def _client(service, region_name=None):
        return {"cloudformation": fake_cfn, "ecr": fake_ecr}[service]

    with mock.patch("pocket_cli.cli.deploy_cli.boto3.client", _client):
        interaction.set_assume_yes(True)
        try:
            cleanup_legacy_container_resources(context)
        finally:
            interaction.set_assume_yes(False)

    fake_cfn.describe_stacks.assert_called_once_with(StackName="dev-testprj-container")
    fake_cfn.delete_stack.assert_called_once_with(StackName="dev-testprj-container")
    fake_ecr.delete_repository.assert_called_once_with(
        repositoryName="dev-testprj-pocket-lambda", force=True
    )


def test_cleanup_legacy_noop_when_absent():
    from pocket_cli.cli.deploy_cli import cleanup_legacy_container_resources

    context = Context.from_settings(
        settings.Settings.model_validate(_two_container_data())
    )
    fake_cfn = mock.MagicMock()

    class _ClientError(Exception):
        pass

    fake_cfn.exceptions.ClientError = _ClientError
    fake_cfn.describe_stacks.side_effect = _ClientError()
    fake_ecr = mock.MagicMock()

    class _NotFound(Exception):
        pass

    fake_ecr.exceptions.RepositoryNotFoundException = _NotFound
    fake_ecr.describe_repositories.side_effect = _NotFound()

    def _client(service, region_name=None):
        return {"cloudformation": fake_cfn, "ecr": fake_ecr}[service]

    with mock.patch("pocket_cli.cli.deploy_cli.boto3.client", _client):
        cleanup_legacy_container_resources(context)

    fake_cfn.delete_stack.assert_not_called()
    fake_ecr.delete_repository.assert_not_called()


def _single_container_secret_data() -> dict:
    data = _base_data()
    data["container"] = {
        "main": {
            "dockerfile_path": "Dockerfile",
            "secrets": {"managed": {"SECRET_KEY": {"type": "password"}}},
        },
    }
    return data


class _FakeStore:
    def __init__(self, secrets: dict):
        self.secrets = dict(secrets)
        self.deleted: set[str] = set()

    def update_secrets(self, new_secrets: dict):
        self.secrets = dict(new_secrets)

    def delete_secret_keys(self, keys):
        self.deleted |= set(keys)
        for key in keys:
            self.secrets.pop(key, None)


def test_mediator_inherits_legacy_value_instead_of_regenerating():
    """0.28 以前の project 共有パスにある値は container store へ copy される。

    SECRET_KEY を再生成すると Django の session / 署名 cookie が無効化される
    ため、旧パスの値を引き継ぐ (自己回復移行)。
    """
    from pocket_cli.mediator import Mediator

    context = Context.from_settings(
        settings.Settings.model_validate(_single_container_secret_data())
    )
    assert context.secrets is not None
    container_sc = context.container["main"].secrets
    assert container_sc is not None
    shared_store = _FakeStore({"SECRET_KEY": "legacy-value"})
    container_store = _FakeStore({})
    object.__setattr__(context.secrets, "pocket_store", shared_store)
    object.__setattr__(container_sc, "pocket_store", container_store)

    Mediator(context).create_pocket_managed_secrets(exists="ignore")

    assert container_store.secrets["SECRET_KEY"] == "legacy-value"
    # 旧パス側はこの時点では消さない (旧 stack の Lambda が読み続けるため)
    assert shared_store.secrets["SECRET_KEY"] == "legacy-value"


def test_mediator_shared_store_tolerates_unshared_residue():
    """shared store の orphan cleanup は移行中の非共有 key を消さない。"""
    from pocket_cli.mediator import Mediator

    context = Context.from_settings(
        settings.Settings.model_validate(_single_container_secret_data())
    )
    assert context.secrets is not None
    container_sc = context.container["main"].secrets
    assert container_sc is not None
    shared_store = _FakeStore({"SECRET_KEY": "legacy-value", "TRULY_ORPHAN": "x"})
    container_store = _FakeStore({"SECRET_KEY": "copied"})
    object.__setattr__(context.secrets, "pocket_store", shared_store)
    object.__setattr__(container_sc, "pocket_store", container_store)

    Mediator(context)._cleanup_orphaned_secrets()

    assert shared_store.deleted == {"TRULY_ORPHAN"}
    assert "SECRET_KEY" in shared_store.secrets


def test_cleanup_legacy_secret_residue_deletes_migrated_keys():
    from pocket_cli.cli import interaction
    from pocket_cli.cli.deploy_cli import cleanup_legacy_secret_residue

    context = Context.from_settings(
        settings.Settings.model_validate(_single_container_secret_data())
    )
    assert context.secrets is not None
    container_sc = context.container["main"].secrets
    assert container_sc is not None
    shared_store = _FakeStore({"SECRET_KEY": "legacy-value"})
    container_store = _FakeStore({"SECRET_KEY": "legacy-value"})
    object.__setattr__(context.secrets, "pocket_store", shared_store)
    object.__setattr__(container_sc, "pocket_store", container_store)

    interaction.set_assume_yes(True)
    try:
        cleanup_legacy_secret_residue(context)
    finally:
        interaction.set_assume_yes(False)

    assert shared_store.deleted == {"SECRET_KEY"}


def test_cleanup_legacy_secret_residue_noop_before_copy():
    """container store へ未コピーのうちは旧パスを消さない。"""
    from pocket_cli.cli.deploy_cli import cleanup_legacy_secret_residue

    context = Context.from_settings(
        settings.Settings.model_validate(_single_container_secret_data())
    )
    assert context.secrets is not None
    container_sc = context.container["main"].secrets
    assert container_sc is not None
    shared_store = _FakeStore({"SECRET_KEY": "legacy-value"})
    container_store = _FakeStore({})
    object.__setattr__(context.secrets, "pocket_store", shared_store)
    object.__setattr__(container_sc, "pocket_store", container_store)

    cleanup_legacy_secret_residue(context)

    assert shared_store.deleted == set()
