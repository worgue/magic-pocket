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
        SimpleNamespace(stage=stage, project_name="myapp", namespace="default"),
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
