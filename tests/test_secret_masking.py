from pocket.utils import mask_secret_values


def test_mask_secret_values_masks_known_keys_recursively():
    """context/settings の表示用 dump から credential 値が消えること

    `pocket resource neon context` 等が api_key を平文で pprint し、session log に
    secret が残りうる問題への回帰テスト (neon status password 漏洩と同型)。
    """
    data = {
        "api_key": "neon-secret",
        "project_name": "dev-myapp",
        "tidb": {"public_key": "pub", "private_key": "priv", "region": "ap-1"},
        "secrets": [{"type": "basic_auth_credential", "options": {"password": "pw"}}],
    }
    masked = mask_secret_values(data)
    assert masked["api_key"] == "****"
    assert masked["project_name"] == "dev-myapp"
    assert masked["tidb"] == {
        "public_key": "****",
        "private_key": "****",
        "region": "ap-1",
    }
    assert masked["secrets"][0]["options"]["password"] == "****"
    # 元データは破壊しない
    assert data["api_key"] == "neon-secret"


def test_mask_secret_values_keeps_none_as_unset_marker():
    assert mask_secret_values({"api_key": None}) == {"api_key": None}
