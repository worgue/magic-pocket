from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from pocket_cli.resources.neon import NeonApi, NeonNotFound


def _fake_response(status_code: int, payload: dict) -> MagicMock:
    res = MagicMock()
    res.status_code = status_code
    res.json.return_value = payload
    return res


def test_neon_api_get_raises_neon_not_found_on_404():
    api = NeonApi("fake-key")
    with patch("pocket.provisioning.neon._http_request") as mock_req:
        mock_req.return_value = _fake_response(404, {"message": "role not found"})
        with pytest.raises(NeonNotFound, match="role not found"):
            api.get("projects/x/branches/y/roles/missing")


def test_neon_api_get_raises_generic_on_500():
    api = NeonApi("fake-key")
    with patch("pocket.provisioning.neon._http_request") as mock_req:
        mock_req.return_value = _fake_response(500, {"message": "internal"})
        with pytest.raises(Exception, match="500: internal") as excinfo:
            api.get("projects/x")
        assert not isinstance(excinfo.value, NeonNotFound)


def test_neon_api_get_returns_response_on_200():
    api = NeonApi("fake-key")
    with patch("pocket.provisioning.neon._http_request") as mock_req:
        mock_req.return_value = _fake_response(200, {"role": {"name": "foo"}})
        res = api.get("projects/x/branches/y/roles/foo")
        assert res.status_code == 200
        assert res.json() == {"role": {"name": "foo"}}


def test_neon_role_returns_none_when_role_missing():
    """branch はあるが role が 404 のとき role プロパティが None を返すこと"""
    from pocket_cli.resources.neon import Branch, Neon

    from pocket.context import NeonContext

    ctx = NeonContext(
        pg_version=15,
        api_key="fake",
        project_name="dev-myapp",
        branch_name="sandbox",
        name="myapp",
        role_name="myapp",
    )
    neon = Neon(ctx)

    # branch は存在する状態をモック
    with (
        patch.object(
            Neon,
            "branch",
            new=Branch(id="br-xxx", name="sandbox"),
        ),
        patch.object(
            Neon,
            "project",
            new=MagicMock(id="mock-project-12345678", name="dev-myapp"),
        ),
        patch("pocket.provisioning.neon._http_request") as mock_req,
    ):
        mock_req.return_value = _fake_response(404, {"message": "role not found"})
        assert neon.role is None


def _fake_root(stage: str):
    """NeonContext.from_settings が参照する root の最小スタブ
    (stage / project_name / namespace を参照する)。"""
    from types import SimpleNamespace
    from typing import cast

    from pocket import settings

    return cast(
        settings.Settings,
        SimpleNamespace(
            stage=stage,
            project_name="myapp",
            namespace="default",
            format_vars={"stage": stage, "project": "myapp", "namespace": "default"},
        ),
    )


def test_neon_context_branch_name_defaults_to_main():
    """branch_name 省略時は stage 名ではなく default ブランチ (main) を使う"""
    from pocket import settings
    from pocket.context import NeonContext

    neon = settings.Neon(project_name="dev-myapp")
    assert neon.branch_name is None
    ctx = NeonContext.from_settings(neon, _fake_root(stage="prod"))
    assert ctx.branch_name == "main"
    assert ctx.parent_branch_name is None


def test_neon_context_records_branch_name_specified_and_stage():
    """from_settings が branch_name の明示有無と stage を context に記録する
    (未指定 fallback の警告判定に使う)。"""
    from pocket import settings
    from pocket.context import NeonContext

    unspecified = NeonContext.from_settings(
        settings.Neon(project_name="dev-myapp"), _fake_root(stage="dev")
    )
    assert unspecified.branch_name_specified is False
    assert unspecified.stage == "dev"

    specified = NeonContext.from_settings(
        settings.Neon(project_name="dev-myapp", branch_name="{stage}"),
        _fake_root(stage="dev"),
    )
    assert specified.branch_name_specified is True


def _branches_payload():
    return {
        "branches": [
            {"id": "br-main", "name": "main"},
            {"id": "br-dev", "name": "dev", "parent_id": "br-main"},
        ]
    }


def _shadowing_ctx(**overrides):
    from pocket.context import NeonContext

    kwargs = {
        "pg_version": 15,
        "api_key": "fake",
        "project_name": "dev-myapp",
        "branch_name": "main",
        "branch_name_specified": False,
        "stage": "dev",
        "name": "myapp",
        "role_name": "myapp",
    }
    kwargs.update(overrides)
    return NeonContext(**kwargs)


def test_neon_warns_when_default_branch_shadows_stage_branch(capsys):
    """branch_name 未指定 (default main) で stage 名 branch が存在したら警告する

    0.5.0 のデフォルト変更 (stage 名 → main) を跨ぐ移行で、store-url が実データと
    別の branch の URL を焼き、アプリが空 DB を参照した実害への回帰テスト
    (2026-07-24 受領の利用プロジェクト feedback)。
    """
    from pocket_cli.resources.neon import Neon

    neon = Neon(_shadowing_ctx())
    with (
        patch.object(Neon, "project", new=MagicMock(id="proj-1", name="dev-myapp")),
        patch("pocket.provisioning.neon._http_request") as mock_req,
    ):
        mock_req.return_value = _fake_response(200, _branches_payload())
        branch = neon.branch
    assert branch is not None
    assert branch.name == "main"
    err = capsys.readouterr().err.replace("\n", "")
    assert "branch_name" in err
    assert "'dev'" in err


def test_neon_no_warning_when_branch_name_specified(capsys):
    """branch_name を明示していれば stage 名 branch が存在しても警告しない"""
    from pocket_cli.resources.neon import Neon

    neon = Neon(_shadowing_ctx(branch_name_specified=True))
    with (
        patch.object(Neon, "project", new=MagicMock(id="proj-1", name="dev-myapp")),
        patch("pocket.provisioning.neon._http_request") as mock_req,
    ):
        mock_req.return_value = _fake_response(200, _branches_payload())
        assert neon.branch is not None
    assert "branch_name" not in capsys.readouterr().err


def test_neon_no_warning_when_stage_branch_absent(capsys):
    """未指定でも stage 名 branch が無ければ警告しない (新規 project の通常運用)"""
    from pocket_cli.resources.neon import Neon

    neon = Neon(_shadowing_ctx(stage="prod"))
    with (
        patch.object(Neon, "project", new=MagicMock(id="proj-1", name="dev-myapp")),
        patch("pocket.provisioning.neon._http_request") as mock_req,
    ):
        mock_req.return_value = _fake_response(200, _branches_payload())
        assert neon.branch is not None
    assert "branch_name" not in capsys.readouterr().err


def test_neon_context_branch_name_override():
    """branch_name を明示すると (per-stage 上書き含む) その値が使われる"""
    from pocket import settings
    from pocket.context import NeonContext

    neon = settings.Neon(project_name="dev-myapp", branch_name="sandbox")
    ctx = NeonContext.from_settings(neon, _fake_root(stage="prod"))
    assert ctx.branch_name == "sandbox"


def test_neon_context_branch_name_template():
    """branch_name は {stage} 等を展開する (動的な feature 環境向け)"""
    from pocket import settings
    from pocket.context import NeonContext

    neon = settings.Neon(project_name="dev-myapp", branch_name="feature-{stage}")
    ctx = NeonContext.from_settings(neon, _fake_root(stage="abc"))
    assert ctx.branch_name == "feature-abc"


def test_neon_context_parent_branch_name_template():
    """parent_branch_name も展開され、指定時のみ値を持つ"""
    from pocket import settings
    from pocket.context import NeonContext

    neon = settings.Neon(
        project_name="dev-myapp",
        branch_name="feature-{stage}",
        parent_branch_name="main",
    )
    ctx = NeonContext.from_settings(neon, _fake_root(stage="abc"))
    assert ctx.parent_branch_name == "main"


def test_neon_parent_branch_resolves_from_project():
    """parent_branch_name 指定時、project 内の同名ブランチを Branch として解決する"""
    from pocket_cli.resources.neon import Branch, Neon

    from pocket.context import NeonContext

    ctx = NeonContext(
        pg_version=15,
        api_key="fake",
        project_name="dev-myapp",
        branch_name="feature-abc",
        parent_branch_name="main",
        name="myapp",
        role_name="myapp",
    )
    neon = Neon(ctx)
    with (
        patch.object(Neon, "project", new=MagicMock(id="proj-1", name="dev-myapp")),
        patch.object(
            Neon,
            "get",
            return_value=_fake_response(
                200, {"branches": [{"id": "br-main", "name": "main"}]}
            ),
        ),
    ):
        parent = neon.parent_branch
        assert isinstance(parent, Branch)
        assert parent.id == "br-main"


def test_neon_parent_branch_none_when_unset():
    """parent_branch_name 未指定なら parent_branch は None (= default 分岐)"""
    from pocket_cli.resources.neon import Neon

    from pocket.context import NeonContext

    ctx = NeonContext(
        pg_version=15,
        api_key="fake",
        project_name="dev-myapp",
        branch_name="feature-abc",
        name="myapp",
        role_name="myapp",
    )
    neon = Neon(ctx)
    # project にも触れずに None を返す (API call 無し)
    with patch.object(Neon, "get") as mock_get:
        assert neon.parent_branch is None
        mock_get.assert_not_called()


def _idempotency_ctx():
    from pocket.context import NeonContext

    return NeonContext(
        pg_version=15,
        api_key="fake",
        project_name="dev-myapp",
        branch_name="main",
        name="myapp",
        role_name="myapp",
    )


def test_neon_create_branch_skips_post_when_branch_exists():
    """既存 branch (default main を含む) があるとき create_branch は POST しない

    Neon project 作成時に自動生成される default main が存在すると、無条件 POST は
    409 (branch already exists) で落ちる。branch が引ける場合はスキップして冪等にする。
    """
    from pocket_cli.resources.neon import Branch, Neon

    neon = Neon(_idempotency_ctx())
    with (
        patch.object(Neon, "branch", new=Branch(id="br-main", name="main")),
        patch.object(Neon, "post") as mock_post,
    ):
        neon.create_branch()
        mock_post.assert_not_called()


def test_neon_create_posts_branch_when_absent():
    """branch が無ければ create_branch は POST して新規作成する (従来動作の維持)"""
    from pocket_cli.resources.neon import Neon

    neon = Neon(_idempotency_ctx())
    # branch/endpoint cached_property を None にしておき、del での cache 無効化を通す
    neon.__dict__["branch"] = None
    neon.__dict__["endpoint"] = None
    with patch.object(Neon, "post") as mock_post:
        neon.create_branch()
        mock_post.assert_called_once()
        assert mock_post.call_args.args[0] == "branches"


def test_neon_create_is_idempotent_when_branch_exists():
    """既存 branch があるとき create() は branch を作らず role/database を ensure する

    default main を使う stage の初回 deploy が 409 にならず、既存 branch 上に
    db/role を bootstrap できることを保証する。
    """
    from pocket_cli.resources.neon import Branch, Neon

    neon = Neon(_idempotency_ctx())
    with (
        patch.object(Neon, "branch", new=Branch(id="br-main", name="main")),
        patch.object(Neon, "create_branch") as mock_create_branch,
        patch.object(Neon, "ensure_role") as mock_ensure_role,
        patch.object(Neon, "ensure_database") as mock_ensure_database,
    ):
        neon.create()
        mock_create_branch.assert_not_called()
        mock_ensure_role.assert_called_once()
        mock_ensure_database.assert_called_once()


def test_neon_resource_reexport_is_same_object():
    """CLI 側の import は runtime package の再エクスポートで同一クラスを指す
    (isinstance / patch.object の互換を保つ)。"""
    import pocket_cli.resources.neon as cli_neon

    from pocket.provisioning import neon as runtime_neon

    assert cli_neon.Neon is runtime_neon.Neon
    assert cli_neon.NeonApi is runtime_neon.NeonApi
    assert cli_neon.ensure_and_compute_url is runtime_neon.ensure_and_compute_url


def test_ensure_and_compute_url_builds_context_and_delegates():
    """公開 API は引数から NeonContext (provisioning='command') を組み立て、
    共有ヘルパ ensure_url_for_context に委譲する。"""
    from pocket.provisioning import neon as runtime_neon

    with patch.object(runtime_neon, "ensure_url_for_context") as mock_ensure:
        mock_ensure.return_value = "postgres://myapp:pw@host:5432/myapp?sslmode=require"
        url = runtime_neon.ensure_and_compute_url(
            project_name="dev-myapp",
            branch_name="sandbox",
            name="myapp",
            role_name="myapp",
            api_key="fake-key",
        )
    assert url == "postgres://myapp:pw@host:5432/myapp?sslmode=require"
    ctx = mock_ensure.call_args.args[0]
    assert ctx.project_name == "dev-myapp"
    assert ctx.branch_name == "sandbox"
    assert ctx.name == "myapp"
    assert ctx.role_name == "myapp"
    assert ctx.api_key == "fake-key"
    assert ctx.parent_branch_name is None
    assert ctx.provisioning == "command"


def test_ensure_url_for_context_skips_branch_create_when_present():
    """既存 branch があるとき ensure_url_for_context は branch を作らず role/db を
    ensure し、fresh instance で算出した database_url を返す。"""
    from pocket.provisioning.neon import Branch, Neon, ensure_url_for_context

    expected = "postgres://myapp:pw@host:5432/myapp?sslmode=require"
    with (
        patch.object(Neon, "branch", new=Branch(id="br-main", name="main")),
        patch.object(Neon, "create_branch") as mock_create_branch,
        patch.object(Neon, "ensure_role") as mock_ensure_role,
        patch.object(Neon, "ensure_database") as mock_ensure_database,
        patch.object(
            Neon, "database_url", new_callable=PropertyMock, return_value=expected
        ),
    ):
        url = ensure_url_for_context(_idempotency_ctx())
    assert url == expected
    mock_create_branch.assert_not_called()
    mock_ensure_role.assert_called_once()
    mock_ensure_database.assert_called_once()


def test_ensure_url_for_context_with_info_reports_resolution():
    """with_info 版が branch / endpoint host (URL から解析) / 既存 branch 利用を
    返すこと。追加 API call なしで store-url の接続先表示に使う。"""
    from pocket.provisioning.neon import Branch, Neon, ensure_url_for_context_with_info

    expected = "postgres://myapp:pw@ep-x.host.example:5432/myapp?sslmode=require"
    with (
        patch.object(Neon, "branch", new=Branch(id="br-main", name="main")),
        patch.object(Neon, "create_branch"),
        patch.object(Neon, "ensure_role"),
        patch.object(Neon, "ensure_database"),
        patch.object(
            Neon, "database_url", new_callable=PropertyMock, return_value=expected
        ),
    ):
        info = ensure_url_for_context_with_info(_idempotency_ctx())
    assert info.url == expected
    assert info.project_name == "dev-myapp"
    assert info.branch_name == "main"
    assert info.endpoint_host == "ep-x.host.example"
    assert info.branch_created is False


def test_ensure_url_for_context_with_info_marks_created_branch():
    """branch をこの実行で新規作成した場合 branch_created=True (空 branch 警告用)"""
    from pocket.provisioning.neon import Neon, ensure_url_for_context_with_info

    expected = "postgres://myapp:pw@host:5432/myapp?sslmode=require"
    with (
        patch.object(Neon, "branch", new=None),
        patch.object(Neon, "parent_branch", new=None),
        patch.object(Neon, "create_branch"),
        patch.object(Neon, "ensure_role"),
        patch.object(Neon, "ensure_database"),
        patch.object(
            Neon, "database_url", new_callable=PropertyMock, return_value=expected
        ),
    ):
        info = ensure_url_for_context_with_info(_idempotency_ctx())
    assert info.branch_created is True


def test_store_url_resolution_echo_shows_branch_and_endpoint(capsys):
    """store-url の解決表示が branch / endpoint を含み、URL (secret) を含まないこと"""
    from pocket_cli.cli.neon_cli import _echo_store_url_resolution

    from pocket.provisioning.neon import EnsureUrlInfo

    info = EnsureUrlInfo(
        url="postgres://myapp:pw@ep-x.host.example:5432/myapp?sslmode=require",
        project_name="dev-myapp",
        branch_name="main",
        endpoint_host="ep-x.host.example",
        branch_created=False,
    )
    _echo_store_url_resolution("dev", info)
    err = capsys.readouterr().err.replace("\n", "")
    assert "stage=dev" in err
    assert "branch=main" in err
    assert "endpoint=ep-x.host.example" in err
    assert "pw" not in err.replace("ep-x", "")  # URL の password を出さない
    assert "新規作成" not in err


def test_store_url_resolution_echo_warns_on_created_branch(capsys):
    """この実行で branch を新規作成した (= 空 DB に接続する) 場合は警告を添える"""
    from pocket_cli.cli.neon_cli import _echo_store_url_resolution

    from pocket.provisioning.neon import EnsureUrlInfo

    info = EnsureUrlInfo(
        url="postgres://myapp:pw@h:5432/myapp?sslmode=require",
        project_name="dev-myapp",
        branch_name="feature-x",
        endpoint_host="h",
        branch_created=True,
    )
    _echo_store_url_resolution("dev", info)
    err = capsys.readouterr().err.replace("\n", "")
    assert "新規作成" in err
    assert "branch_name" in err


def test_ensure_url_for_context_creates_branch_when_absent():
    """branch が無いとき ensure_url_for_context は parent から branch を作成する。"""
    from pocket.provisioning.neon import Neon, ensure_url_for_context

    expected = "postgres://myapp:pw@host:5432/myapp?sslmode=require"
    with (
        patch.object(Neon, "branch", new=None),
        patch.object(Neon, "parent_branch", new=None),
        patch.object(Neon, "create_branch") as mock_create_branch,
        patch.object(Neon, "ensure_role") as mock_ensure_role,
        patch.object(Neon, "ensure_database") as mock_ensure_database,
        patch.object(
            Neon, "database_url", new_callable=PropertyMock, return_value=expected
        ),
    ):
        url = ensure_url_for_context(_idempotency_ctx())
    assert url == expected
    mock_create_branch.assert_called_once()
    mock_ensure_role.assert_called_once()
    mock_ensure_database.assert_called_once()


def test_neon_endpoint_prefers_read_write():
    """read replica (read_only) が先に並んでいても read_write endpoint を選ぶこと

    endpoint type を見ないと一覧の並び次第で database_url が read_only ホストに
    なり、アプリの書き込みが全滅する (回帰テスト)。
    """
    from pocket_cli.resources.neon import Branch, Neon

    from pocket.context import NeonContext

    ctx = NeonContext(
        pg_version=15,
        api_key="fake",
        project_name="dev-myapp",
        branch_name="sandbox",
        name="myapp",
        role_name="myapp",
    )
    neon = Neon(ctx)
    endpoints = {
        "endpoints": [
            {
                "id": "ep-ro",
                "host": "ro.example",
                "branch_id": "br-xxx",
                "autoscaling_limit_min_cu": 0.25,
                "autoscaling_limit_max_cu": 0.25,
                "type": "read_only",
            },
            {
                "id": "ep-rw",
                "host": "rw.example",
                "branch_id": "br-xxx",
                "autoscaling_limit_min_cu": 0.25,
                "autoscaling_limit_max_cu": 0.25,
                "type": "read_write",
            },
        ]
    }
    with (
        patch.object(Neon, "branch", new=Branch(id="br-xxx", name="sandbox")),
        patch.object(
            Neon,
            "project",
            new=MagicMock(id="mock-project-12345678", name="dev-myapp"),
        ),
        patch("pocket.provisioning.neon._http_request") as mock_req,
    ):
        mock_req.return_value = _fake_response(200, endpoints)
        endpoint = neon.endpoint
    assert endpoint is not None
    assert endpoint.type == "read_write"
    assert endpoint.host == "rw.example"


def test_neon_api_error_without_json_body_reports_status():
    """非 JSON body (LB 由来の 502 HTML 等) でも本来の HTTP エラーが报告されること

    以前は res.json()["message"] 直アクセスで JSONDecodeError / KeyError になり
    本来のエラーを隠していた。
    """
    from pocket.provisioning.neon import _HttpResponse

    api = NeonApi("napi_dummy")
    with patch("pocket.provisioning.neon._http_request") as mock_req:
        mock_req.return_value = _HttpResponse(502, b"<html>Bad Gateway</html>")
        with pytest.raises(Exception, match="502"):
            api.get("projects")


def test_neon_api_401_with_none_key_hints_missing_env(capsys):
    """api_key 未設定 (None) の 401 で TypeError にならず設定不足を案内すること"""
    api = NeonApi(None)
    with patch("pocket.provisioning.neon._http_request") as mock_req:
        mock_req.return_value = _fake_response(401, {"message": "unauthorized"})
        with pytest.raises(Exception, match="401"):
            api.get("projects")
    out = capsys.readouterr().out
    assert "NEON_API_KEY" in out


def test_neon_destroy_plan_branch_for_non_root():
    """非 root branch (parent_id あり) は従来どおり branch 単位で削除できる"""
    from pocket_cli.resources.neon import Branch, Neon

    neon = Neon(_idempotency_ctx())
    with patch.object(
        Neon, "branch", new=Branch(id="br-x", name="sandbox", parent_id="br-main")
    ):
        assert neon.destroy_plan() == "branch"


def test_neon_destroy_plan_project_for_sole_root():
    """root branch が project 内で単独なら project ごと削除する計画になる

    root は branch 単位で削除できない (422: cannot delete the root branch) ため、
    branch delete を試みると destroy が異常終了していた (回帰テスト)。
    """
    from pocket_cli.resources.neon import Branch, Neon

    root = Branch(id="br-main", name="main")
    neon = Neon(_idempotency_ctx())
    with (
        patch.object(Neon, "branch", new=root),
        patch.object(Neon, "branches", new_callable=PropertyMock, return_value=[root]),
    ):
        assert neon.destroy_plan() == "project"


def test_neon_destroy_plan_blocked_when_root_has_siblings():
    """root branch でも他 branch が同居するなら project 削除は巻き添えになるため
    blocked (何も消さない)"""
    from pocket_cli.resources.neon import Branch, Neon

    root = Branch(id="br-main", name="main")
    sibling = Branch(id="br-x", name="sandbox", parent_id="br-main")
    neon = Neon(_idempotency_ctx())
    with (
        patch.object(Neon, "branch", new=root),
        patch.object(
            Neon, "branches", new_callable=PropertyMock, return_value=[root, sibling]
        ),
    ):
        assert neon.destroy_plan() == "blocked"


def test_neon_destroy_plan_raises_when_branch_missing():
    """branch 不在で destroy_plan を呼んだら NeonNotFound (呼び出し側の前提確認)"""
    from pocket_cli.resources.neon import Neon

    neon = Neon(_idempotency_ctx())
    with patch.object(Neon, "branch", new=None):
        with pytest.raises(NeonNotFound):
            neon.destroy_plan()


def test_neon_delete_project_deletes_by_project_id():
    """delete_project は DELETE /projects/{project_id} を叩く"""
    from pocket_cli.resources.neon import Neon, NeonApi

    neon = Neon(_idempotency_ctx())
    with (
        patch.object(Neon, "project", new=MagicMock(id="proj-1", name="dev-myapp")),
        patch.object(NeonApi, "delete") as mock_delete,
    ):
        neon.delete_project()
    mock_delete.assert_called_once_with("projects/proj-1")


def test_neon_branch_parses_parent_id_from_api_listing():
    """branch cached_property が API 一覧の parent_id を保持する (root 判定に必要)"""
    from pocket_cli.resources.neon import Neon

    from pocket.context import NeonContext

    ctx = NeonContext(
        pg_version=15,
        api_key="fake",
        project_name="dev-myapp",
        branch_name="sandbox",
        name="myapp",
        role_name="myapp",
    )
    neon = Neon(ctx)
    branches = {
        "branches": [
            {"id": "br-main", "name": "main"},
            {"id": "br-x", "name": "sandbox", "parent_id": "br-main"},
        ]
    }
    with (
        patch.object(Neon, "project", new=MagicMock(id="proj-1", name="dev-myapp")),
        patch("pocket.provisioning.neon._http_request") as mock_req,
    ):
        mock_req.return_value = _fake_response(200, branches)
        branch = neon.branch
    assert branch is not None
    assert branch.parent_id == "br-main"


def test_neon_database_url_percent_encodes_credentials():
    """password の特殊文字が percent-encode され、解析側の unquote と対称なこと"""
    from pocket_cli.resources.neon import Neon, Role

    from pocket.context import NeonContext
    from pocket.django.db_url import parse_database_url_credentials
    from pocket.provisioning.neon import Endpoint

    ctx = NeonContext(
        pg_version=15,
        api_key="fake",
        project_name="dev-myapp",
        branch_name="sandbox",
        name="myapp",
        role_name="myapp",
    )
    neon = Neon(ctx)
    with (
        patch.object(Neon, "role", new=Role(name="myapp", password="p%40ss w:rd")),
        patch.object(
            Neon,
            "endpoint",
            new=Endpoint(
                id="ep-rw",
                host="h.example",
                autoscaling_limit_min_cu=0.25,
                autoscaling_limit_max_cu=0.25,
                type="read_write",
            ),
        ),
    ):
        url = neon.database_url
    creds = parse_database_url_credentials(url)
    assert creds["PASSWORD"] == "p%40ss w:rd"
    assert creds["USER"] == "myapp"
    assert creds["HOST"] == "h.example"
    assert creds["NAME"] == "myapp"


def test_neon_masked_database_url_hides_password_and_skips_reveal():
    """status 表示用の masked URL が password を含まず、reveal API も呼ばないこと

    `pocket resource neon status` が password 込み URL を出力して session log に
    secret が残った実害への回帰テスト (2026-07-24 受領の利用プロジェクト feedback)。
    """
    from pocket_cli.resources.neon import Neon, Role

    from pocket.context import NeonContext
    from pocket.provisioning.neon import Endpoint

    ctx = NeonContext(
        pg_version=15,
        api_key="fake",
        project_name="dev-myapp",
        branch_name="sandbox",
        name="myapp",
        role_name="myapp",
    )
    neon = Neon(ctx)
    with (
        patch.object(Neon, "role", new=Role(name="myapp", password=None)),
        patch.object(
            Neon,
            "endpoint",
            new=Endpoint(
                id="ep-rw",
                host="h.example",
                autoscaling_limit_min_cu=0.25,
                autoscaling_limit_max_cu=0.25,
                type="read_write",
            ),
        ),
        patch("pocket.provisioning.neon._http_request") as mock_req,
    ):
        url = neon.masked_database_url
    assert url == "postgres://myapp:****@h.example:5432/myapp?sslmode=require"
    mock_req.assert_not_called()
