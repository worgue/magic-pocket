"""CloudFront.ensure_post_deploy_state のテスト。

CF distribution の伝播 timeout 後に KVS 書き込みが永久に取り残される事故 (b12488d
時点の bug) を防ぐためのフック。stack の status に応じて bucket policy / KVS 書き込みを
冪等に再実行することを検証する。
"""

from moto import mock_aws
from pocket_cli.resources.cloudfront import CloudFront

from pocket.context import Context


class _FakeStack:
    def __init__(self, status: str):
        self.status = status


def _install_fake_stack(monkeypatch, status: str):
    """CloudFront.stack を固定 status の FakeStack に差し替える。"""
    fake = _FakeStack(status)
    monkeypatch.setattr(CloudFront, "stack", property(lambda self: fake))


def _patch_post_deploy_steps(monkeypatch, cf):
    """post-deploy で呼ばれる内部メソッドをカウンタに差し替える。"""
    called = {"prepare": 0, "policy": 0, "kvs": 0}

    def _prep(_mediator):
        called["prepare"] += 1

    monkeypatch.setattr(cf, "_prepare_token_secret", _prep)
    monkeypatch.setattr(
        cf,
        "_ensure_bucket_policy",
        lambda: called.__setitem__("policy", called["policy"] + 1),
    )
    monkeypatch.setattr(
        cf,
        "_write_token_secret_to_kvs",
        lambda: called.__setitem__("kvs", called["kvs"] + 1),
    )
    return called


@mock_aws
def test_ensure_post_deploy_state_skips_when_stack_not_completed(use_toml, monkeypatch):
    """stack が COMPLETED でない場合、post-deploy 処理を実行しないこと。"""
    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev")
    assert context.cloudfront
    cf = CloudFront(list(context.cloudfront.values())[0])
    _install_fake_stack(monkeypatch, "PROGRESS")
    called = _patch_post_deploy_steps(monkeypatch, cf)

    cf.ensure_post_deploy_state(mediator=None)

    # prepare (store からの読み込みのみ・副作用なし) は status 判定前に走るが、
    # 変更系 (bucket policy / KVS) は実行されないこと
    assert called["policy"] == 0
    assert called["kvs"] == 0


@mock_aws
def test_ensure_post_deploy_state_runs_when_stack_completed(use_toml, monkeypatch):
    """stack が COMPLETED の場合、bucket policy / KVS 書き込みが再実行されること。

    これにより update() の wait_status が timeout した後の deploy でも、
    KVS 書き込みが冪等に復旧する (b12488d の bug 再発防止)。
    """
    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev")
    assert context.cloudfront
    cf = CloudFront(list(context.cloudfront.values())[0])
    _install_fake_stack(monkeypatch, "COMPLETED")
    called = _patch_post_deploy_steps(monkeypatch, cf)

    cf.ensure_post_deploy_state(mediator=None)

    assert called == {"prepare": 1, "policy": 1, "kvs": 1}


@mock_aws
def test_ensure_post_deploy_state_is_idempotent(use_toml, monkeypatch):
    """同じフックを 2 回呼んでも例外が出ず、各 step が単純に 2 回実行されること。"""
    use_toml("tests/data/toml/default.toml")
    context = Context.from_toml(stage="dev")
    assert context.cloudfront
    cf = CloudFront(list(context.cloudfront.values())[0])
    _install_fake_stack(monkeypatch, "COMPLETED")
    called = _patch_post_deploy_steps(monkeypatch, cf)

    cf.ensure_post_deploy_state(mediator=None)
    cf.ensure_post_deploy_state(mediator=None)

    assert called == {"prepare": 2, "policy": 2, "kvs": 2}
