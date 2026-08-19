from __future__ import annotations

from typing import TYPE_CHECKING

from pocket.utils import echo
from pocket_cli.mediator import Mediator
from pocket_cli.resources.aws.cloudformation import CloudFrontWafStack
from pocket_cli.resources.aws.stack_backed import StackBackedResource

if TYPE_CHECKING:
    from pocket.context import CloudFrontContext


class CloudFrontWaf(StackBackedResource):
    """us-east-1 に WAFv2 IPSet + WebACL を管理するリソース。

    `[cloudfront.<name>.waf]` block がある CloudFront でのみ使用。
    IPSet の中身は `pocket waf ip ...` CLI で管理する (side-channel)。
    allow_rules の header secret は managed secret 経路で自動生成し、
    WebACL のルールに焼き込む。
    """

    context: CloudFrontContext

    def __init__(self, context: CloudFrontContext) -> None:
        super().__init__(context)
        self._allow_secret_values: dict[str, str] = {}

    @property
    def description(self):
        return "Create WAFv2 IPSet + WebACL in us-east-1 for: %s" % self.context.name

    def state_info(self):
        key = "cloudfront-waf-%s" % self.context.name
        return {key: {"name": self.context.name}}

    @property
    def stack(self):
        return CloudFrontWafStack(
            self.context, allow_secret_values=self._allow_secret_values
        )

    def prepare_deploy(self, mediator: Mediator | None = None):
        """template hash に影響する allow secret を store から読み込む (副作用なし)。

        status / yaml_synced の判定前に呼ぶこと。空のまま hash を計算すると
        deploy 済み hash と一致せず、毎回 REQUIRE_UPDATE になる。
        """
        if mediator is None:
            return
        self._prepare_allow_secrets(mediator)

    # mediator を取るのは意図的な非対称。deploy フロー (_deploy_resource) は
    # inspect.signature で mediator の有無を見て呼び分ける
    def create(self, mediator: Mediator | None = None):  # type: ignore[override]
        echo.log("WAF (IPSet + WebACL) を作成中 (us-east-1)...")
        self._ensure_allow_secrets(mediator)
        self._echo_allow_rules()
        self._create_stack()
        if self.context.waf is None:
            raise RuntimeError("waf context is not configured")
        if self.context.waf.enable_ip_set:
            echo.info(
                "WAF を作成しました。`pocket waf ip add self --name %s --stage %s` "
                "で自分の IP を allowlist に追加してください "
                "(空の状態では deny-all になります)。"
                % (self.context.name, self.context.stage)
            )
        else:
            echo.info("WAF を作成しました (IP allowlist 無効、managed rules のみ)。")

    def update(self, mediator: Mediator | None = None):  # type: ignore[override]
        self._ensure_allow_secrets(mediator)
        self._echo_allow_rules()
        self._update_stack()

    def delete(self):
        echo.log("WAF スタックを削除中...")
        self._delete_stack()

    def _ensure_allow_secrets(self, mediator: Mediator | None):
        """allow secret を生成してから読み込む (create/update 用)。

        deploy 順は WAF → Container のため、初回はまだ secret が無い。
        CloudFrontKeys と同じく mediator 経由で先に生成する。
        """
        waf = self.context.waf
        if not waf or not waf.header_secret_keys:
            return
        if mediator is None:
            raise RuntimeError(
                "waf.allow_rules の header secret を解決できません。"
                "`pocket deploy` から実行してください。"
            )
        mediator.ensure_pocket_managed_secrets()
        self._prepare_allow_secrets(mediator)
        missing = [
            key
            for key in waf.header_secret_keys
            if key not in self._allow_secret_values
        ]
        if missing:
            raise RuntimeError(
                "waf allow secret が managed secrets に見つかりません: %s"
                % ", ".join(missing)
            )

    def _prepare_allow_secrets(self, mediator: Mediator):
        waf = self.context.waf
        if not waf or not waf.header_secret_keys:
            return
        sc = mediator.context.secrets
        if sc is None:
            # settings 検証済みのため通常到達しない
            echo.warning("container secrets is not configured.")
            return
        secrets = sc.pocket_store.secrets
        values = {}
        for key in waf.header_secret_keys:
            value = secrets.get(key)
            if isinstance(value, str):
                values[key] = value
        self._allow_secret_values = values

    def _echo_allow_rules(self):
        """allow_rules は WAF を弱める宣言なので、deploy のたびに一覧を可視化する。"""
        waf = self.context.waf
        if not waf or not waf.allow_rules:
            return
        echo.info(
            "WAF allow_rules (%d 件、IPSet / managed rules より先に評価):"
            % len(waf.allow_rules)
        )
        for i, rule in enumerate(waf.allow_rules):
            parts = []
            if rule.path:
                parts.append("path=%s" % rule.path)
            if rule.header:
                parts.append("header secret=%s" % rule.header)
            echo.info("  allow-%d: %s" % (i, " AND ".join(parts)))
