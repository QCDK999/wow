"""Email notifications over SMTP.

Send a clearly-labelled TEST email to check the SMTP secrets:

    python -m monitor.notifier --test
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import smtplib
import ssl
import sys
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any
from zoneinfo import ZoneInfo

from . import config
from .check_stock import AVAILABLE, POSSIBLY_AVAILABLE

log = logging.getLogger("vca.notify")
ET = ZoneInfo("America/New_York")

SUBJECT_AVAILABLE = "🎉 VCA 五花手链可以在线购买了！"
SUBJECT_POSSIBLY = "🔔 VCA 手链疑似补货，请人工确认"
SUBJECT_POSSIBLY_STRONG = "🔔🔔 VCA 手链强烈疑似补货（库存接口显示有货），请立即人工确认"
SUBJECT_HEALTH_DOWN = "⚠️ VCA Stock Monitor 监控失效：无法判断库存"
SUBJECT_HEALTH_RECOVERED = "✅ VCA Stock Monitor 监控已恢复"
SUBJECT_TEST = "[测试邮件] VCA Stock Monitor 邮件配置测试"

HEALTH_DOWN = "HEALTH_DOWN"
HEALTH_RECOVERED = "HEALTH_RECOVERED"


class NotificationError(RuntimeError):
    pass


def _et(ts: datetime | str | None) -> str:
    if ts is None:
        return "—"
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return ts
    return ts.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S %Z（美国东部时间）")


def _run_url() -> str:
    server, repo, run = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else "（本地运行）"


def build_message(kind: str, settings: config.Settings, checked_at: datetime,
                  evidence: list[str], context: dict[str, Any] | None = None) -> EmailMessage:
    ctx = context or {}
    basis = "\n".join(f"  • {e}" for e in evidence) or "  • （无）"
    owed_note = ("\n注意：这是之前一次检测发现的结果。当时邮件未能成功发送，之后的检测暂时无法访问官网，"
                 "因此补发此通知。请以官网当前页面为准。\n" if ctx.get("owed") else "")
    if kind == AVAILABLE:
        subject = SUBJECT_AVAILABLE
        body = f"""VCA Stock Monitor 检测到你关注的手链可能已经恢复在线购买！
{owed_note}
商品：{config.PRODUCT_NAME}
商品编号：{config.PRODUCT_REF}
当前状态：AVAILABLE
检测时间：{_et(checked_at)}
检测依据：
{basis}

官网购买链接：
{config.PRODUCT_URL}

请尽快前往官网确认并购买。

库存状态可能随时变化，最终以官网实际结账结果为准。
"""
    elif kind == POSSIBLY_AVAILABLE:
        strong = ctx.get("strength") == "strong"
        n = ctx.get("number", 1)
        reason = ctx.get("reason", "first")
        subject = SUBJECT_POSSIBLY_STRONG if strong else SUBJECT_POSSIBLY
        if reason == "reminder":
            subject += f"（第 {n} 次提醒）"
        intro = {
            "first": "VCA Stock Monitor 发现了可能补货的信号，但【尚未验证可以成功在线购买】。",
            "escalation": "【升级提醒】疑似补货的证据变强了：页面上所有库存信号都显示有货，"
                          "只是自动加入购物袋的验证没有通过。【尚未验证可以成功在线购买】。",
            "reminder": f"【第 {n} 次提醒】疑似补货的状态仍在持续（首次发现：{_et(ctx.get('first_seen_at'))}），"
                        "但【尚未验证可以成功在线购买】。",
        }.get(reason, "")
        body = f"""{intro}
{owed_note}
商品：{config.PRODUCT_NAME}
商品编号：{config.PRODUCT_REF}
当前状态：POSSIBLY_AVAILABLE（疑似补货，需人工确认）
证据强度：{"强（库存接口、按钮、页面状态均显示有货）" if strong else "弱（信号不一致）"}
检测时间：{_et(checked_at)}
检测依据：
{basis}

请打开官网人工确认：
{config.PRODUCT_URL}

如果之后确认可以购买，系统会再发送一封正式的补货通知。
疑似状态持续时，系统会有限次数地再次提醒（不会每小时发送）。
"""
    elif kind == HEALTH_DOWN:
        h = ctx.get("health") or {}
        subject = SUBJECT_HEALTH_DOWN
        kind_text = ("官网拦截了自动访问（验证码 / HTTP 403 等）" if h.get("last_kind") == "blocked"
                     else "页面加载失败或页面结构发生变化")
        body = f"""VCA Stock Monitor 已连续 {h.get('consecutive_unknown', '?')} 次无法判断库存状态。
在恢复之前，即使手链补货，你也【可能收不到通知】。

可能原因：{kind_text}
开始时间：{_et(h.get('first_unknown_at'))}
最近一次成功检测：{_et(ctx.get('last_ok_check_at'))}
最近一次原因：{h.get('last_reason') or '—'}
运行记录：{_run_url()}

这不代表手链无货。在此期间请自行查看官网：
{config.PRODUCT_URL}

问题持续时，本提醒最多每 24 小时发送一次；恢复后会发送一封恢复通知。
处理建议见 README「监控健康」一节（例如降低检测频率，或改用常开设备上的 self-hosted runner）。
"""
    elif kind == HEALTH_RECOVERED:
        subject = SUBJECT_HEALTH_RECOVERED
        body = f"""VCA Stock Monitor 已恢复正常，可以再次判断库存状态。

故障开始：{_et(ctx.get('down_since'))}
恢复时间：{_et(checked_at)}
当前检测依据：
{basis}
"""
    elif kind == "TEST":
        subject = SUBJECT_TEST
        body = f"""这是一封【测试邮件】，不是补货通知。

VCA Stock Monitor 的 SMTP 配置可以正常发送邮件。
发送时间：{_et(checked_at)}

监控商品：{config.PRODUCT_NAME}（{config.PRODUCT_REF}）
"""
    else:
        raise ValueError(f"unknown notification kind {kind!r}")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.sender
    msg["To"] = settings.notify_to
    msg["Date"] = formatdate(localtime=False)
    # A stable Message-ID for the same logical notification lets providers such
    # as Gmail drop an exact re-send (e.g. after a crash between send and confirm).
    key = ctx.get("dedupe_key")
    if key:
        digest = hashlib.sha256(f"{config.PRODUCT_REF}|{key}".encode()).hexdigest()[:32]
        msg["Message-ID"] = f"<{digest}@vca-stock-monitor.local>"
    else:
        msg["Message-ID"] = make_msgid(domain="vca-stock-monitor.local")
    if kind == AVAILABLE or (kind == POSSIBLY_AVAILABLE and ctx.get("strength") == "strong"):
        msg["X-Priority"] = "1"
        msg["Importance"] = "high"
    msg.set_content(body)
    return msg


def _send_once(msg: EmailMessage, s: config.Settings) -> None:
    ctx = ssl.create_default_context()
    if s.smtp_security == "ssl":
        with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ctx, timeout=30) as smtp:
            smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            if s.smtp_security == "starttls":
                smtp.starttls(context=ctx)
                smtp.ehlo()
            smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(msg)


def send(kind: str, settings: config.Settings, checked_at: datetime, evidence: list[str],
         attempts: int = 3, backoff_s: float = 10.0, context: dict[str, Any] | None = None) -> None:
    """Send with limited retries. Raises NotificationError (never leaks secrets)."""
    if not settings.email_configured:
        raise NotificationError("邮件未配置：缺少 SMTP_HOST / SMTP_USERNAME / SMTP_PASSWORD / NOTIFY_TO")
    msg = build_message(kind, settings, checked_at, evidence, context)
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            _send_once(msg, settings)
            log.info("email sent (%s)", kind)
            return
        except smtplib.SMTPAuthenticationError as e:
            # Retrying a bad password will not help.
            raise NotificationError(f"SMTP 登录失败 (code {e.smtp_code})") from None
        except Exception as e:
            last = e
            log.warning("email attempt %d/%d failed: %s", i, attempts, type(e).__name__)
            if i < attempts:
                time.sleep(backoff_s * i)
    raise NotificationError(f"邮件发送失败: {type(last).__name__}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="VCA Stock Monitor email")
    ap.add_argument("--test", action="store_true", help="send a clearly-labelled test email")
    ap.add_argument("--check-config", action="store_true", help="only report whether email is configured")
    args = ap.parse_args(argv)
    s = config.load_settings()
    print(f"email configured: {s.email_configured} (host set: {bool(s.smtp_host)}, port: {s.smtp_port}, "
          f"security: {s.smtp_security}, user set: {bool(s.smtp_username)}, "
          f"password set: {bool(s.smtp_password)}, recipient set: {bool(s.notify_to)})")
    if args.check_config or not args.test:
        return 0 if s.email_configured else 1
    try:
        send("TEST", s, datetime.now(ET), [], attempts=2, backoff_s=5)
    except NotificationError as e:
        print(f"TEST EMAIL FAILED: {e}")
        return 1
    print("TEST EMAIL SENT – check the inbox (and spam folder) of NOTIFY_TO")
    return 0


if __name__ == "__main__":
    sys.exit(main())
