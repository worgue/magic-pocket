"""create_bucket の削除伝播待ち再試行 (OperationAborted) のテスト (KN1454)。

region 移設で旧 region の bucket を destroy した直後に同名で deploy すると、
S3 の削除伝播が終わるまで CreateBucket が OperationAborted を返す。CLI が
待って再試行し、他のエラーは即座に再送出することを固定する。
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError
from pocket_cli.resources.aws import s3_utils


class _FakeClient:
    def __init__(self, failures: int, code: str = "OperationAborted"):
        self.failures = failures
        self.code = code
        self.calls: list[dict] = []

    def create_bucket(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) <= self.failures:
            raise ClientError(
                {"Error": {"Code": self.code, "Message": "conflicting operation"}},
                "CreateBucket",
            )
        return {}


def test_retries_operation_aborted_until_success(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(s3_utils.time, "sleep", sleeps.append)
    client = _FakeClient(failures=2)

    s3_utils.create_bucket(
        client, "b", "ap-northeast-1", retry_interval=7, retry_timeout=3600
    )

    assert len(client.calls) == 3
    assert sleeps == [7, 7]
    assert client.calls[0]["CreateBucketConfiguration"] == {
        "LocationConstraint": "ap-northeast-1"
    }


def test_other_client_error_is_raised_immediately(monkeypatch):
    monkeypatch.setattr(
        s3_utils.time, "sleep", lambda _s: pytest.fail("sleep してはいけない")
    )
    client = _FakeClient(failures=1, code="BucketAlreadyExists")

    with pytest.raises(ClientError):
        s3_utils.create_bucket(client, "b", "ap-northeast-1")
    assert len(client.calls) == 1


def test_gives_up_after_timeout_with_guidance(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(s3_utils.time, "monotonic", lambda: clock["now"])

    def _sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(s3_utils.time, "sleep", _sleep)
    client = _FakeClient(failures=100)

    with pytest.raises(RuntimeError, match="削除の伝播"):
        s3_utils.create_bucket(
            client, "b", "us-east-1", retry_interval=30, retry_timeout=120
        )
    # 0s, 30s, 60s, 90s, 120s(超過) → 5 回試行
    assert len(client.calls) == 5
    assert "CreateBucketConfiguration" not in client.calls[0]
