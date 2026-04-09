from unittest.mock import MagicMock, patch

import pytest
from pocket_cli.resources.neon import NeonApi, NeonNotFound


def _fake_response(status_code: int, payload: dict) -> MagicMock:
    res = MagicMock()
    res.status_code = status_code
    res.json.return_value = payload
    return res


def test_neon_api_get_raises_neon_not_found_on_404():
    api = NeonApi("fake-key")
    with patch("requests.get") as mock_get:
        mock_get.return_value = _fake_response(404, {"message": "role not found"})
        with pytest.raises(NeonNotFound, match="role not found"):
            api.get("projects/x/branches/y/roles/missing")


def test_neon_api_get_raises_generic_on_500():
    api = NeonApi("fake-key")
    with patch("requests.get") as mock_get:
        mock_get.return_value = _fake_response(500, {"message": "internal"})
        with pytest.raises(Exception, match="500: internal") as excinfo:
            api.get("projects/x")
        assert not isinstance(excinfo.value, NeonNotFound)


def test_neon_api_get_returns_response_on_200():
    api = NeonApi("fake-key")
    with patch("requests.get") as mock_get:
        mock_get.return_value = _fake_response(200, {"role": {"name": "foo"}})
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
        project_name="dev-signage",
        branch_name="sandbox",
        name="signage",
        role_name="signage",
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
            new=MagicMock(id="odd-sea-97456112", name="dev-signage"),
        ),
        patch("requests.get") as mock_get,
    ):
        mock_get.return_value = _fake_response(404, {"message": "role not found"})
        assert neon.role is None
