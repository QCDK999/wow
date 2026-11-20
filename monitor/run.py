"""One monitoring run: check → decide → claim → notify → confirm → publish status.

    python -m monitor.run --data-dir data [--git] [--force] [--test-email]

Exit codes (the GitHub Actions run turns red on anything but 0):
  0  ok – including a *single* UNKNOWN result (shown as a ::warning::)
  1  state could not be saved (restock emails were still sent – see below)
  2  a restock email (AVAILABLE / POSSIBLY_AVAILABLE) failed; retried next run
  3  the monitor is unhealthy: HEALTH_ALERT_AFTER consecutive UNKNOWN results.
     Returned only on runs that issue a health alert (first one, then at most
     every HEALTH_REMIND_HOURS) so GitHub's failure emails don't arrive hourly.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import config, notifier
from .check_stock import AVAILABLE, POSSIBLY_AVAILABLE, UNAVAILABLE, UNKNOWN, CheckResult, run_check
from .state import (HEALTH_DOWN, STOCK_KINDS, FileStateStore, GitBranchStateStore, SaveResult, Send,
                    StateStore, confirm, decide, is_degraded)

log = logging.getLogger("vca.run")

EXIT_OK, EXIT_STATE, EXIT_NOTIFY, EXIT_UNHEALTHY = 0, 1, 2, 3
STATUS_SCHEMA = 1
STATUS_LABEL = {
    "AVAILABLE": "Available",
    "POSSIBLY_AVAILABLE": "Possibly Available",
    "UNAVAILABLE": "Unavailable",
    "UNKNOWN": "Unknown",
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def build_status(state: dict[str, Any], settings: config.Settings, now: datetime) -> dict[str, Any]:
    """Public status.json. Contains NO addresses, hosts or secrets."""
    email = state.get("email", {})
    health = state.get("health", {})
    stock_status = state.get("last_status") or UNKNOWN
    n_unknown = health.get("consecutive_unknown", 0)

    problems: list[str] = []
    if stock_status == UNKNOWN and state.get("last_check_at"):
        why = "官网拦截了自动访问" if health.get("last_kind") == "blocked" else "页面加载失败或结构变化"
        problems.append(f"连续 {n_unknown} 次无法判断库存（{why}）")
    if not settings.email_configured:
        problems.append("邮件通知尚未配置")
    err, sent = email.get("last_error_at"), email.get("last_sent_at")
    if err and (not sent or err > sent):
        problems.append("最近一次通知邮件发送失败")

    if settings.monitor_paused:
        mon_state, message = "paused", "监控已暂停（仓库变量 MONITOR_PAUSED=true）"
    elif problems:
        mon_state, message = "error", "；".join(problems)
    else:
        mon_state, message = "active", "监控运行正常"

    interval = settings.check_interval_minutes
    return {
        "schema": STATUS_SCHEMA,
        "generated_at": _iso(now),
        "product": {
            "name": config.PRODUCT_NAME,
            "ref": config.PRODUCT_REF,
            "material": config.PRODUCT_MATERIAL,
            "url": config.PRODUCT_URL,
            "region": "United States",
        },
        "monitoring": {
            "state": mon_state,
            "message": message,
            "interval_minutes": interval,
            # The site should consider data stale after this moment.
            "stale_after": _iso(now + timedelta(minutes=interval * 2 + 30)),
            "consecutive_unknown": n_unknown,
            "last_ok_check_at": state.get("last_ok_check_at"),
            "health_alert_active": bool(health.get("alert_active")),
        },
        "stock": {
            "status": stock_status,
            "label": STATUS_LABEL.get(stock_status, "Unknown"),
            "checked_at": state.get("last_check_at"),
            "evidence": state.get("last_evidence", []),
        },
        "notifications": {
            "configured": settings.email_configured,
            "last_sent_at": sent,
            "last_kind": email.get("last_kind"),
            "last_error_at": err,
            "last_test_ok_at": email.get("last_test_ok_at"),
            "last_test_error_at": email.get("last_test_error_at"),
        },
    }


def _status_file(state, settings, now) -> dict[str, str]:
    return {"status.json": json.dumps(build_status(state, settings, now), indent=2, ensure_ascii=False) + "\n"}


def _save_with_retry(store: StateStore, mutate: Callable[[dict], dict], settings, now_fn, message,
                     attempts: int = 6, sleep: Callable[[float], None] = time.sleep) -> dict | None:
    """Reload → mutate → save, retrying conflicts immediately and errors with backoff."""
    for i in range(attempts):
        state, version = store.load()
        new = mutate(state)
        r = store.save(new, version, _status_file(new, settings, now_fn()), message)
        if r is SaveResult.OK:
            return new
        if r is SaveResult.ERROR:
            sleep(min(5 * 2 ** i, 60))
        log.info("save %s – retrying (%d/%d)", r.value, i + 1, attempts)
    return None


def needs_check(state: dict[str, Any], settings: config.Settings, now: datetime) -> bool:
    """Skip a run only when everything is calm and the interval has not elapsed."""
    if state.get("pending") or state["health"]["consecutive_unknown"]:
        return True
    if state.get("last_status") != UNAVAILABLE:
        return True  # positive, owed or unknown → never skip
    last = state.get("last_check_at")
    try:
        age = now - datetime.fromisoformat(last) if last else None
    except ValueError:
        return True
    # 25 min slack: hourly cron runs drift; a late run must not make the next skip.
    return age is None or age >= timedelta(minutes=settings.check_interval_minutes - 25)


def ping_healthcheck(settings: config.Settings, ok: bool) -> None:
    if not settings.healthcheck_url:
        return
    url = settings.healthcheck_url.rstrip("/") + ("" if ok else "/fail")
    try:
        urllib.request.urlopen(url, timeout=10).read()
    except Exception as e:  # never let the heartbeat break the monitor
        log.warning("healthcheck ping failed: %s", type(e).__name__)


def _safe_check(checker, settings) -> CheckResult:
    try:
        return checker(settings)
    except Exception as e:  # a crashed checker is an UNKNOWN, never a silent pass
        log.exception("checker crashed")
        return CheckResult(UNKNOWN, [f"检测程序异常: {type(e).__name__}: {str(e)[:200]}"],
                           datetime.now(timezone.utc).isoformat(timespec="seconds"), {},
                           unknown_kind="error")


def run(store: StateStore, settings: config.Settings, *, force: bool = False,
        checker: Callable[[config.Settings], CheckResult] = run_check,
        sender: Callable[..., None] = notifier.send,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        run_id: str | None = None, result_out: str | None = None,
        sleep: Callable[[float], None] = time.sleep) -> int:
    run_id = run_id or os.environ.get("GITHUB_RUN_ID") or uuid.uuid4().hex[:12]
    policy = settings.policy

    if settings.monitor_paused:
        log.info("monitor paused – publishing status only")
        ok = _save_with_retry(store, lambda s: s, settings, now_fn, "monitor: paused", sleep=sleep)
        return EXIT_OK if ok else EXIT_STATE

    state, _ = store.load()
    if not force and not needs_check(state, settings, now_fn()):
        log.info("interval not elapsed and nothing pending – skipping (no site access)")
        ping_healthcheck(settings, True)
        return EXIT_OK

    result = _safe_check(checker, settings)
    log.info("result: %s | %s", result.status, " | ".join(result.evidence))
    if result_out:
        Path(result_out).write_text(result.to_json(), encoding="utf-8")
    check_time = datetime.fromisoformat(result.checked_at)

    # 1) record result + claim notifications atomically
    decision = None
    persisted = False
    for i in range(6):
        state, version = store.load()
        decision = decide(state, result.status, check_time, run_id, result.evidence, policy=policy,
                          strength=result.strength, unknown_kind=result.unknown_kind)
        r = store.save(decision.state, version, _status_file(decision.state, settings, now_fn()),
                       f"monitor: {result.status}")
        if r is SaveResult.OK:
            persisted = True
            break
        if r is SaveResult.ERROR:
            sleep(min(5 * 2 ** i, 60))
    sends = decision.sends if decision else []
    if not persisted:
        # We could not record a claim. Restock emails are still sent – a possible
        # duplicate is far better than a missed restock. Health mails are not
        # (they would repeat every hour while the state cannot be written).
        sends = decision.stock_sends if decision else []
        print("::error::State could not be saved; sending restock notifications anyway (duplicates possible)")

    if result.status == UNKNOWN:
        n = decision.state["health"]["consecutive_unknown"] if decision else "?"
        print(f"::warning::VCA check UNKNOWN ({n} in a row, {result.unknown_kind}): "
              f"{'; '.join(result.evidence)[:400]}")

    # 2) send – restock first, then health mail
    outcomes: list[tuple[Send, bool, str | None]] = []
    for s in sorted(sends, key=lambda s: s.kind not in STOCK_KINDS):
        try:
            sender(s.kind, settings, s.checked_at, s.evidence, context=s.context)
            outcomes.append((s, True, None))
            log.info("sent %s", s.kind)
        except Exception as e:  # NotificationError or anything unexpected
            outcomes.append((s, False, str(e)))
            log.error("notification %s failed: %s", s.kind, e)
            print(f"::error::VCA notification ({s.kind}) failed: {e}")

    # 3) confirm / release claims
    saved = True
    if outcomes:
        def mutate(st):
            for s, ok, err in outcomes:
                st = confirm(st, s, ok, now_fn(), run_id, err)
            return st
        if persisted:
            saved = _save_with_retry(store, mutate, settings, now_fn,
                                     "monitor: notifications " + ", ".join(
                                         f"{s.kind}={'sent' if ok else 'failed'}" for s, ok, _ in outcomes),
                                     sleep=sleep) is not None
        else:
            saved = False
        if not saved:
            print("::error::Notification result could not be saved – the next run may send a duplicate")

    final_state = store.load()[0]
    degraded = is_degraded(final_state, policy)
    ping_healthcheck(settings, persisted and saved and not degraded)

    if any(not ok and s.kind in STOCK_KINDS for s, ok, _ in outcomes):
        return EXIT_NOTIFY
    if not persisted or not saved:
        return EXIT_STATE
    if any(s.kind == HEALTH_DOWN for s, _, _ in outcomes):
        print(f"::error::Monitor unhealthy: {final_state['health']['consecutive_unknown']} consecutive "
              f"UNKNOWN results – restocks may be missed until this is fixed")
        return EXIT_UNHEALTHY
    return EXIT_OK


def send_test_email(store: StateStore, settings: config.Settings,
                    sender: Callable[..., None] = notifier.send, sleep: Callable[[float], None] = time.sleep) -> int:
    now = datetime.now(timezone.utc)
    ok = True
    try:
        sender("TEST", settings, now, [], attempts=2, backoff_s=5)
        print("TEST EMAIL SENT")
    except Exception as e:
        ok = False
        print(f"::error::TEST EMAIL FAILED: {e}")

    def mutate(s):
        s["email"]["last_test_ok_at" if ok else "last_test_error_at"] = _iso(now)
        return s

    saved = _save_with_retry(store, mutate, settings, lambda: now,
                             f"monitor: test email {'ok' if ok else 'failed'}", sleep=sleep)
    return 0 if ok and saved else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--git", action="store_true", help="data dir is a git work-tree on the data branch")
    ap.add_argument("--branch", default="monitor-data")
    ap.add_argument("--force", action="store_true", help="ignore the interval (manual runs)")
    ap.add_argument("--test-email", action="store_true")
    ap.add_argument("--result-out", default=None, help="write raw check result JSON here")
    args = ap.parse_args(argv)

    settings = config.load_settings()
    store: StateStore = (GitBranchStateStore(args.data_dir, args.branch) if args.git
                         else FileStateStore(args.data_dir))
    if args.test_email:
        return send_test_email(store, settings)
    return run(store, settings, force=args.force, result_out=args.result_out)


if __name__ == "__main__":
    sys.exit(main())
