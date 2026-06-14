"""`pocket waf ip ...` — IPSet を CFN を介さず直接更新する CLI。

WebACL / IPSet 自体は CFn (CloudFrontWafStack) が us-east-1 に作成し、
Addresses は CFn template 上は常に空。実際の CIDR 一覧はこの CLI が
`update_ip_set` boto3 で side-channel 更新する (1 件追加で CFn を回したくない
ため)。CFn 視点では Addresses は常に drift 状態だが、これは仕様。
"""

from __future__ import annotations

import urllib.request

import boto3
import click

from pocket.context import CloudFrontContext, Context
from pocket.utils import echo
from pocket_cli.resources.aws.cloudformation import CloudFrontWafStack


@click.group()
def waf():
    pass


@waf.group()
def ip():
    pass


def _get_cf_context(stage: str, name: str) -> CloudFrontContext:
    context = Context.from_toml(stage=stage)
    if not context.cloudfront:
        raise click.ClickException("cloudfront is not configured for this stage")
    if name not in context.cloudfront:
        raise click.ClickException("cloudfront '%s' is not configured" % name)
    cf_ctx = context.cloudfront[name]
    if cf_ctx.waf is None:
        raise click.ClickException(
            "cloudfront '%s' has no [cloudfront.%s.waf] block" % (name, name)
        )
    if not cf_ctx.waf.enable_ip_set:
        raise click.ClickException(
            "cloudfront '%s' is configured with [cloudfront.%s.waf]"
            " enable_ip_set = false, so there is no IPSet to operate on."
            " Set `enable_ip_set = true` (default) to use `pocket waf ip ...`."
            % (name, name)
        )
    return cf_ctx


def _get_ip_set_meta(cf_ctx: CloudFrontContext) -> tuple[str, str]:
    """CFn stack output から IPSet の (Name, Id) を取得する。"""
    stack = CloudFrontWafStack(cf_ctx)
    output = stack.output
    if not output:
        raise click.ClickException(
            "WAF stack '%s' が見つかりません。先に `pocket deploy` を実行してください。"
            % stack.name
        )
    name = output.get("IPSetName")
    set_id = output.get("IPSetId")
    if not name or not set_id:
        raise click.ClickException(
            "WAF stack output に IPSetName / IPSetId がありません: %s" % stack.name
        )
    return name, set_id


def _wafv2_client():
    # Scope=CLOUDFRONT は us-east-1 でしか操作できない
    return boto3.client("wafv2", region_name="us-east-1")


def _fetch_ip_set(client, set_name: str, set_id: str) -> tuple[list[str], str]:
    res = client.get_ip_set(Name=set_name, Scope="CLOUDFRONT", Id=set_id)
    return list(res["IPSet"]["Addresses"]), res["LockToken"]


def _write_ip_set(client, set_name: str, set_id: str, addresses: list[str], lock: str):
    client.update_ip_set(
        Name=set_name,
        Scope="CLOUDFRONT",
        Id=set_id,
        Addresses=addresses,
        LockToken=lock,
    )


def _detect_self_ipv4() -> str:
    """1 次: AWS checkip、fallback: ipify。/32 を付けた CIDR を返す。"""
    for url in ("https://checkip.amazonaws.com", "https://api.ipify.org"):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 固定 URL (checkip/ipify) + timeout 済
                ip = r.read().decode("utf-8").strip()
            if ip:
                return f"{ip}/32"
        except Exception as e:  # noqa: BLE001
            echo.warning("IP 検出失敗 (%s): %s" % (url, e))
    raise click.ClickException("自分の Global IP を取得できませんでした")


@ip.command("list")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", required=True, help="CloudFront name (pocket.toml の key)")
def ip_list(stage, name):
    """IPSet 内 CIDR 一覧を表示する。"""
    cf_ctx = _get_cf_context(stage, name)
    set_name, set_id = _get_ip_set_meta(cf_ctx)
    client = _wafv2_client()
    addresses, _ = _fetch_ip_set(client, set_name, set_id)
    if not addresses:
        echo.warning(
            "IPSet は空です (deny-all 状態)。"
            "`pocket waf ip add self --name %s --stage %s` で自分の IP を"
            "追加してください。" % (name, stage)
        )
        return
    for cidr in addresses:
        click.echo(cidr)


@ip.command("add")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", required=True, help="CloudFront name (pocket.toml の key)")
@click.argument("cidr")
def ip_add(stage, name, cidr):
    """任意 CIDR を IPSet に追加。`self` で自分の IP を追加 (重複は dedup)。"""
    cf_ctx = _get_cf_context(stage, name)
    set_name, set_id = _get_ip_set_meta(cf_ctx)
    client = _wafv2_client()
    if cidr == "self":
        cidr = _detect_self_ipv4()
        echo.info("検出した自分の IP: %s" % cidr)
    addresses, lock = _fetch_ip_set(client, set_name, set_id)
    if cidr in addresses:
        echo.info("%s は既に登録済みです。" % cidr)
        return
    new_addresses = addresses + [cidr]
    _write_ip_set(client, set_name, set_id, new_addresses, lock)
    echo.success("追加しました: %s (合計 %d entries)" % (cidr, len(new_addresses)))


@ip.command("remove")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", required=True, help="CloudFront name (pocket.toml の key)")
@click.argument("cidr")
def ip_remove(stage, name, cidr):
    """指定 CIDR を IPSet から削除。"""
    cf_ctx = _get_cf_context(stage, name)
    set_name, set_id = _get_ip_set_meta(cf_ctx)
    client = _wafv2_client()
    addresses, lock = _fetch_ip_set(client, set_name, set_id)
    if cidr not in addresses:
        echo.warning("%s は IPSet に存在しません。" % cidr)
        return
    new_addresses = [a for a in addresses if a != cidr]
    _write_ip_set(client, set_name, set_id, new_addresses, lock)
    echo.success("削除しました: %s (残り %d entries)" % (cidr, len(new_addresses)))


@ip.command("clear")
@click.option("--stage", envvar="POCKET_DEPLOY_STAGE", prompt=True)
@click.option("--name", required=True, help="CloudFront name (pocket.toml の key)")
@click.option("--yes", is_flag=True, default=False, help="確認プロンプトをスキップ")
def ip_clear(stage, name, yes):
    """IPSet を空にする (= deny-all 状態に戻す)。確認プロンプトあり。"""
    cf_ctx = _get_cf_context(stage, name)
    set_name, set_id = _get_ip_set_meta(cf_ctx)
    client = _wafv2_client()
    addresses, lock = _fetch_ip_set(client, set_name, set_id)
    if not addresses:
        echo.info("IPSet は既に空です。")
        return
    if not yes:
        click.confirm(
            "%d 件の CIDR を全削除して deny-all 状態にします。よろしいですか？"
            % len(addresses),
            abort=True,
        )
    _write_ip_set(client, set_name, set_id, [], lock)
    echo.success("IPSet を全削除しました (deny-all 状態)。")
