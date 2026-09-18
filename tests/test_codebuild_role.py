"""CodeBuild サービスロールの命名と、0.36 以前の旧名ロールの掃除。"""

from botocore.stub import Stubber
from pocket_cli.resources.aws.builders.codebuild import CodeBuildBuilder

ROLE = "dev-testprj-pocket-codebuild-role"
LEGACY_ROLE = "forge-dev-testprj-pocket-codebuild-role"


def _builder() -> CodeBuildBuilder:
    # __init__ は boto3 client を作るだけ (AWS 通信なし)
    return CodeBuildBuilder(
        region="us-east-1",
        resource_prefix="dev-testprj-pocket-",
        state_bucket="test-bucket",
    )


def _add_role_delete(iam: Stubber, role_name: str) -> None:
    iam.add_response(
        "list_role_policies",
        {"PolicyNames": ["codebuild-policy"]},
        {"RoleName": role_name},
    )
    iam.add_response(
        "delete_role_policy",
        {},
        {"RoleName": role_name, "PolicyName": "codebuild-policy"},
    )
    iam.add_response("delete_role", {}, {"RoleName": role_name})


def test_role_name_follows_resource_prefix():
    """ロール名は他リソースと同じ {resource_prefix} 規約 (forge- を付けない)。"""
    assert _builder()._role_name == ROLE


def test_delete_removes_current_and_legacy_roles():
    """destroy は現行名に加え、旧版で作られた forge- 付きロールも消す。"""
    builder = _builder()
    codebuild = Stubber(builder.codebuild)
    codebuild.add_response(
        "delete_project", {}, {"name": "dev-testprj-pocket-codebuild"}
    )
    iam = Stubber(builder.iam)
    _add_role_delete(iam, ROLE)
    _add_role_delete(iam, LEGACY_ROLE)
    with codebuild, iam:
        builder.delete()
    iam.assert_no_pending_responses()


def test_delete_legacy_role_is_noop_when_absent():
    builder = _builder()
    iam = Stubber(builder.iam)
    iam.add_client_error(
        "list_role_policies",
        service_error_code="NoSuchEntity",
        expected_params={"RoleName": LEGACY_ROLE},
    )
    with iam:
        builder._delete_role(LEGACY_ROLE)  # raise しない
    iam.assert_no_pending_responses()


def test_role_exists_detects_legacy_only_stage():
    """旧版で deploy したままの stage も destroy の対象として検出する。"""
    builder = _builder()
    iam = Stubber(builder.iam)
    iam.add_client_error(
        "get_role",
        service_error_code="NoSuchEntity",
        expected_params={"RoleName": ROLE},
    )
    iam.add_response(
        "get_role",
        {
            "Role": {
                "Path": "/",
                "RoleName": LEGACY_ROLE,
                "RoleId": "AROAEXAMPLEEXAMPLE123",
                "Arn": "arn:aws:iam::123456789012:role/%s" % LEGACY_ROLE,
                "CreateDate": "2026-01-01T00:00:00Z",
            }
        },
        {"RoleName": LEGACY_ROLE},
    )
    with iam:
        assert builder.role_exists() is True
