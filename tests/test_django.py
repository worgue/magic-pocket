from pathlib import Path

import pytest
from click.testing import CliRunner
from pocket_cli.django_cli import (
    _get_management_command_handler,
    _staticfiles_link,
    _staticfiles_publish_mode,
    collectstatic_locally,
    deploystatic,
    upload_collected_staticfiles,
)

from pocket import settings
from pocket.context import Context, SesContext
from pocket.django import utils as django_utils
from pocket.django.context import DjangoStorageContext
from pocket.django.utils import (
    _tidb_ca_bundle_path,
    get_caches,
    get_databases,
    get_email_backend,
    get_storages,
)


def test_manage_cli(base_settings, aws_settings):
    s = base_settings
    with pytest.raises(Exception, match="container is not configured .*"):
        _get_management_command_handler(Context.from_settings(s))
    s.container["main"] = aws_settings
    handler = _get_management_command_handler(Context.from_settings(s))
    assert handler
    m = s.container["main"].handlers.pop("management")
    with pytest.raises(Exception, match="Add management command handler for this .*"):
        _get_management_command_handler(Context.from_settings(s))
    s.container["main"].handlers["m1"] = m
    s.container["main"].handlers["m2"] = m
    with pytest.raises(Exception, match="Only one management command handler is .*"):
        settings.Settings.model_validate(s.model_dump())


def test_storages(use_toml):
    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev")
    assert context.container["main"] and context.container["main"].django
    assert context.container["main"].django.storages == {
        "default": DjangoStorageContext(
            store="s3",
            location="media",
            static=False,
            manifest=False,
            distribution=None,
            route=None,
        ),
        "staticfiles": DjangoStorageContext(
            store="s3",
            location="static",
            static=True,
            manifest=True,
            distribution=None,
            route=None,
        ),
    }
    storages = get_storages(stage="dev")
    assert storages == {
        "default": {
            "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
            "OPTIONS": {"bucket_name": "dev-testprj-pocket", "location": "media"},
        },
        "staticfiles": {
            "BACKEND": "storages.backends.s3boto3.S3ManifestStaticStorage",
            "OPTIONS": {"bucket_name": "dev-testprj-pocket", "location": "static"},
        },
    }


def test_s3_route_no_origin_path_single_prefix(use_toml):
    """origin_path 省略の S3 route は単一 prefix (media/) になる。

    origin_path を指定した route は従来どおり origin_path + path_pattern の
    二重階層 (static/static/) のまま (opt-in で単一化できる)。
    """
    use_toml("tests/data/toml/cloudfront_s3_route_no_origin_path.toml")
    storages = get_storages(stage="dev")
    # media: origin_path 省略 → location は path_pattern 由来の "media" のみ。
    # 従来の "media/media" のような二重階層にならない。
    assert storages["default"]["OPTIONS"]["location"] == "media"
    # static: origin_path="/static" 指定 → 従来どおり origin_path + path_pattern。
    assert storages["staticfiles"]["OPTIONS"]["location"] == "static/static"


def test_bucket_policy_prefix_includes_empty_origin_route(use_toml):
    """OAC バケットポリシーの prefix 計算に origin_path 省略 route も含まれる。

    spa=/app/spa, static=/app/static は /app 配下だが media は origin_path 省略で
    /media 配下。media を含めれば共通 prefix は "" (バケット全体許可) になり media/ が
    読める。含めないと共通 prefix が "/app" になり media/ が 403 になってしまう。
    """
    use_toml("tests/data/toml/cloudfront_s3_route_bucket_policy.toml")
    context = Context.from_toml(stage="dev")
    cf = context.cloudfront["web"]
    assert cf.bucket_policy_prefix == ""


def test_cache(use_toml):
    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev")
    assert (
        context.container["main"]
        and context.container["main"].django
        and context.container["main"].django.caches["default"]
        and context.container["main"].vpc
        and context.container["main"].vpc.efs
    )
    assert context.container["main"].vpc.efs.local_mount_path == "/mnt/efs"
    assert context.container["main"].django.caches["default"].model_dump() == {
        "store": "efs",
        "location_subdir": "{stage}",
        "location": "/mnt/efs/dev",
    }
    caches = get_caches(stage="dev")
    assert caches == {
        "default": {
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
            "LOCATION": "/mnt/efs/dev",
        }
    }


def test_ses_context(use_toml):
    use_toml("tests/data/toml/ses.toml")
    context = Context.from_toml(stage="dev")
    assert context.ses == SesContext(
        from_email="noreply@example.com",
        region="ap-northeast-1",
        configuration_set=None,
    )
    assert context.container["main"]
    assert context.container["main"].use_ses is True


def test_ses_context_custom_region(use_toml):
    use_toml("tests/data/toml/ses_custom_region.toml")
    context = Context.from_toml(stage="dev")
    assert context.ses == SesContext(
        from_email="noreply@example.com",
        region="us-east-1",
        configuration_set="my-config-set",
    )


def test_ses_email_backend(use_toml):
    use_toml("tests/data/toml/ses.toml")
    result = get_email_backend(stage="dev")
    assert result == {
        "EMAIL_BACKEND": "django_ses.SESBackend",
        "DEFAULT_FROM_EMAIL": "noreply@example.com",
        "AWS_SES_REGION_NAME": "ap-northeast-1",
    }


def test_ses_email_backend_with_configuration_set(use_toml):
    use_toml("tests/data/toml/ses_custom_region.toml")
    result = get_email_backend(stage="dev")
    assert result == {
        "EMAIL_BACKEND": "django_ses.SESBackend",
        "DEFAULT_FROM_EMAIL": "noreply@example.com",
        "AWS_SES_REGION_NAME": "us-east-1",
        "AWS_SES_CONFIGURATION_SET": "my-config-set",
    }


def test_ses_email_backend_not_configured(use_toml):
    use_toml("tests/data/toml/default.toml")
    result = get_email_backend(stage="dev")
    assert result == {}


def test_staticfiles_publish_default_is_deploy(use_toml):
    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev")
    assert context.container["main"] and context.container["main"].django
    assert context.container["main"].django.storages["staticfiles"].publish == "deploy"
    assert _staticfiles_publish_mode(context) == "deploy"


def test_staticfiles_publish_command(use_toml):
    use_toml("tests/data/toml/staticfiles_publish_command.toml")
    context = Context.from_toml(stage="dev")
    assert context.container["main"] and context.container["main"].django
    assert context.container["main"].django.storages["staticfiles"].publish == "command"
    assert _staticfiles_publish_mode(context) == "command"


def test_upload_collected_staticfiles_delete_opt_in(use_toml, monkeypatch):
    use_toml("tests/data/toml/default.toml")
    cmds = []
    monkeypatch.setattr("pocket_cli.django_cli.run", lambda cmd, **kw: cmds.append(cmd))
    upload_collected_staticfiles("dev")
    assert "--delete" not in cmds[0]
    upload_collected_staticfiles("dev", delete=True)
    # cmd はリスト引数 (shell=False)。パスにスペースが含まれても分解されない
    assert cmds[1][-1] == "--delete"


def test_collectstatic_locally_link_opt_in(use_toml, monkeypatch):
    use_toml("tests/data/toml/default.toml")
    cmds = []
    monkeypatch.setattr("pocket_cli.django_cli.run", lambda cmd, **kw: cmds.append(cmd))
    collectstatic_locally("dev")
    assert "--link" not in cmds[0]
    collectstatic_locally("dev", link=True)
    assert "--link" in cmds[1]


def test_staticfiles_link_declared(use_toml):
    """staticfiles 宣言の link は context に伝播し、未宣言の既定は False"""
    use_toml("tests/data/toml/staticfiles_link.toml")
    context = Context.from_toml(stage="dev")
    assert context.container["main"] and context.container["main"].django
    assert context.container["main"].django.storages["staticfiles"].link is True
    assert _staticfiles_link(context) is True

    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev")
    assert _staticfiles_link(context) is False


def test_collectstatic_locally_link_clears_build_dir(use_toml, monkeypatch):
    """link 時のみビルド先を事前クリアする。

    非 link で作られた実体との混在 (collectstatic の全量再コピー) と、
    ソース削除で残る壊れ symlink (aws s3 sync が exit 2 で失敗) を避ける。
    非 link 時は従来どおりクリアしない (実体の全量再コピーを避ける)。
    """
    use_toml("tests/data/toml/default.toml")
    removed = []
    monkeypatch.setattr("pocket_cli.django_cli.run", lambda cmd, **kw: None)
    monkeypatch.setattr(
        "pocket_cli.django_cli.shutil.rmtree",
        lambda path, ignore_errors=False: removed.append(str(path)),
    )
    collectstatic_locally("dev")
    assert removed == []
    collectstatic_locally("dev", link=True)
    assert len(removed) == 1
    assert removed[0].endswith(str(Path("pocket_cache") / "static_build" / "dev"))


def test_deploystatic_link_falls_back_to_declaration(use_toml, monkeypatch):
    """deploystatic はフラグ省略時に pocket.toml の link 宣言へフォールバックし、
    --no-link で宣言を上書きできる"""
    use_toml("tests/data/toml/staticfiles_link.toml")
    calls = []
    monkeypatch.setattr(
        "pocket_cli.django_cli.collectstatic_locally",
        lambda stage, link=False: calls.append(link),
    )
    monkeypatch.setattr(
        "pocket_cli.django_cli.upload_collected_staticfiles",
        lambda stage, delete=False: None,
    )
    runner = CliRunner()
    result = runner.invoke(deploystatic, ["--stage", "dev"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(deploystatic, ["--stage", "dev", "--no-link"])
    assert result.exit_code == 0, result.output
    assert calls == [True, False]


def test_ses_not_configured_use_ses_false(use_toml):
    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev")
    assert context.ses is None
    assert context.container["main"]
    assert context.container["main"].use_ses is False


def test_tidb_ca_bundle_path_prefers_al2023(monkeypatch):
    # AL2023 と Debian 両方が存在する場合は AL2023 (先頭候補) を選ぶ。
    monkeypatch.setattr(django_utils.os.path, "exists", lambda p: True)
    assert _tidb_ca_bundle_path() == "/etc/pki/tls/certs/ca-bundle.crt"


def test_tidb_ca_bundle_path_falls_back_to_debian(monkeypatch):
    # AL2023 の ca-bundle.crt が無く Debian 命名だけある環境 (開発環境等)。
    monkeypatch.setattr(
        django_utils.os.path,
        "exists",
        lambda p: p == "/etc/ssl/certs/ca-certificates.crt",
    )
    assert _tidb_ca_bundle_path() == "/etc/ssl/certs/ca-certificates.crt"


def test_tidb_ca_bundle_path_default_when_none_exist(monkeypatch):
    # どの候補も無ければ実行基盤 (AL2023) の標準パスを最後の拠り所に返す。
    monkeypatch.setattr(django_utils.os.path, "exists", lambda p: False)
    assert _tidb_ca_bundle_path() == "/etc/pki/tls/certs/ca-bundle.crt"


def test_get_databases_tidb_ssl_and_persistent_conn(monkeypatch):
    # tidb backend では TLS CA を候補探索で解決し、Lambda 向けに持続接続を
    # 標準デフォルト化する。
    monkeypatch.setattr(django_utils, "_detect_engine", lambda *a, **k: "django_tidb")
    monkeypatch.setattr(django_utils.os.path, "exists", lambda p: True)
    monkeypatch.setenv("DATABASE_URL", "mysql://u:p@gateway.tidbcloud.com:4000/testdb")

    databases = get_databases(stage="dev")
    db = databases["default"]

    assert db["ENGINE"] == "django_tidb"
    assert db["OPTIONS"] == {
        "ssl_mode": "VERIFY_IDENTITY",
        "ssl": {"ca": "/etc/pki/tls/certs/ca-bundle.crt"},
    }
    assert db["CONN_MAX_AGE"] is None
    assert db["CONN_HEALTH_CHECKS"] is True
