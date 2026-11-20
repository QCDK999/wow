"""Regression tests for the reliability fixes: health alerts, POSSIBLY reminders,
owed notifications, claim→send→confirm failure paths and git store errors."""

import json
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from monitor import config, notifier
from monitor.check_stock import AVAILABLE, POSSIBLY_AVAILABLE, UNAVAILABLE, UNKNOWN, CheckResult
from monitor.run import EXIT_NOTIFY, EXIT_OK, EXIT_STATE, EXIT_UNHEALTHY, run
from monitor.state import (HEALTH_DOWN, HEALTH_RECOVERED, FileStateStore, GitBranchStateStore, SaveResult,
                           normalize)

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
S = config.Settings(smtp_host="smtp.example.com", smtp_username="u", smtp_password="p", notify_to="me@example.com")
NOSLEEP = lambda s: None  # noqa: E731


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def tick(self, minutes=60):
        self.t += timedelta(minutes=minutes)


class Sender:
    def __init__(self, fail_kinds=()):
        self.calls, self.fail_kinds = [], set(fail_kinds)

    def __call__(self, kind, settings, checked_at, evidence, context=None, **kw):
        if kind in self.fail_kinds:
            raise notifier.NotificationError("SMTP down")
        self.calls.append({"kind": kind, "at": checked_at, "evidence": evidence, "context": context or {}})

    @property
    def kinds(self):
        return [c["kind"] for c in self.calls]


def result(clock, status, strength=None, kind=None, evidence=None):
    return lambda s: CheckResult(status, evidence or [f"fake {status}"], clock().isoformat(), {},
                                 strength=strength, unknown_kind=kind)


def go(store, clock, status, sender, settings=S, **kw):
    return run(store, settings, checker=result(clock, status, **kw), sender=sender, now_fn=clock, sleep=NOSLEEP)


def status_json(path):
    return json.loads((path / "status.json").read_text())


# ---------------------------------------------------------------- 1. health ---

def test_health_alert_after_three_unknown_then_daily_then_recovery(tmp_path):
    store, clock, sender = FileStateStore(tmp_path), Clock(), Sender()
    codes = []
    for _ in range(50):                       # 50 hours of UNKNOWN (site blocked)
        codes.append(go(store, clock, UNKNOWN, sender, kind="blocked"))
        clock.tick()
    assert codes[:2] == [EXIT_OK, EXIT_OK]    # one or two UNKNOWN: warning only
    assert codes[2] == EXIT_UNHEALTHY         # third in a row: alert + red run
    # first alert on the 3rd UNKNOWN (hour 2), one reminder 24 h later, nothing in between
    assert sender.kinds == [HEALTH_DOWN, HEALTH_DOWN]
    assert [i for i, c in enumerate(codes) if c == EXIT_UNHEALTHY] == [2, 26]
    assert sender.calls[0]["context"]["health"]["consecutive_unknown"] == 3
    st = status_json(tmp_path)
    assert st["monitoring"]["state"] == "error" and st["monitoring"]["consecutive_unknown"] == 50
    assert "拦截" in st["monitoring"]["message"]
    assert st["stock"]["status"] == UNKNOWN   # never shown as Unavailable

    assert go(store, clock, UNAVAILABLE, sender) == EXIT_OK
    assert sender.kinds[-1] == HEALTH_RECOVERED
    clock.tick()
    go(store, clock, UNAVAILABLE, sender)
    assert sender.kinds.count(HEALTH_RECOVERED) == 1
    st = status_json(tmp_path)
    assert st["monitoring"]["state"] == "active" and st["monitoring"]["consecutive_unknown"] == 0


def test_health_counts_reset_by_any_successful_check(tmp_path):
    store, clock, sender = FileStateStore(tmp_path), Clock(), Sender()
    for s in [UNKNOWN, UNKNOWN, UNAVAILABLE, UNKNOWN, UNKNOWN, UNAVAILABLE]:
        go(store, clock, s, sender)
        clock.tick()
    assert sender.kinds == []


def test_health_alert_email_failure_is_retried_but_stock_unaffected(tmp_path):
    store, clock = FileStateStore(tmp_path), Clock()
    bad = Sender(fail_kinds={HEALTH_DOWN})
    for _ in range(3):
        go(store, clock, UNKNOWN, bad)
        clock.tick()
    assert store.load()[0]["health"]["alert_active"] is True
    good = Sender()
    assert go(store, clock, UNKNOWN, good) == EXIT_UNHEALTHY
    assert good.kinds == [HEALTH_DOWN]


def test_checker_crash_counts_as_unknown(tmp_path):
    store, clock, sender = FileStateStore(tmp_path), Clock(), Sender()

    def boom(s):
        raise RuntimeError("playwright missing")

    for _ in range(3):
        code = run(store, S, checker=boom, sender=sender, now_fn=clock, sleep=NOSLEEP)
        clock.tick()
    assert code == EXIT_UNHEALTHY and sender.kinds == [HEALTH_DOWN]


def test_unknown_streak_never_resets_restock_episode(tmp_path):
    store, clock, sender = FileStateStore(tmp_path), Clock(), Sender()
    go(store, clock, AVAILABLE, sender)
    for _ in range(30):
        clock.tick()
        go(store, clock, UNKNOWN, sender)
    clock.tick()
    go(store, clock, AVAILABLE, sender)
    assert sender.kinds.count(AVAILABLE) == 1


# ---------------------------------------------------- 2. POSSIBLY reminders ---

def test_possibly_reminders_are_limited(tmp_path):
    store, clock, sender = FileStateStore(tmp_path), Clock(), Sender()
    go(store, clock, UNAVAILABLE, sender)
    clock.tick()
    hours = []
    for h in range(150):                      # bag check keeps failing for ~6 days
        go(store, clock, POSSIBLY_AVAILABLE, sender, strength="weak")
        if sender.calls and len(hours) < len(sender.calls):
            hours.append(h)
        clock.tick()
    assert sender.kinds == [POSSIBLY_AVAILABLE] * 6          # hard cap
    assert hours == [0, 2, 8, 32, 56, 80]                    # +2h, +6h, then daily
    nums = [c["context"]["number"] for c in sender.calls]
    assert nums == [1, 2, 3, 4, 5, 6]
    assert [c["context"]["reason"] for c in sender.calls][1:] == ["reminder"] * 5


def test_first_possibly_lost_in_spam_still_gets_reminders(tmp_path):
    # "Lost" = SMTP accepted it, the user never saw it. Reminders cover that.
    store, clock, sender = FileStateStore(tmp_path), Clock(), Sender()
    for _ in range(9):
        go(store, clock, POSSIBLY_AVAILABLE, sender)
        clock.tick()
    assert len(sender.calls) == 3


def test_escalation_from_weak_to_strong_is_immediate_once(tmp_path):
    store, clock, sender = FileStateStore(tmp_path), Clock(), Sender()
    go(store, clock, POSSIBLY_AVAILABLE, sender, strength="weak")
    clock.tick()
    go(store, clock, POSSIBLY_AVAILABLE, sender, strength="strong")   # 1 h later: escalate now
    clock.tick()
    go(store, clock, POSSIBLY_AVAILABLE, sender, strength="strong")   # no spam
    assert [c["context"]["reason"] for c in sender.calls] == ["first", "escalation"]
    msg = notifier.build_message(POSSIBLY_AVAILABLE, S, T0, [], sender.calls[1]["context"])
    assert "强烈疑似" in msg["Subject"] and msg["Importance"] == "high"


def test_possibly_failed_send_is_retried(tmp_path):
    store, clock = FileStateStore(tmp_path), Clock()
    assert go(store, clock, POSSIBLY_AVAILABLE, Sender(fail_kinds={POSSIBLY_AVAILABLE})) == EXIT_NOTIFY
    clock.tick()
    s = Sender()
    go(store, clock, POSSIBLY_AVAILABLE, s)
    assert s.kinds == [POSSIBLY_AVAILABLE] and s.calls[0]["context"]["number"] == 1


def test_no_possibly_after_available_was_sent(tmp_path):
    store, clock, sender = FileStateStore(tmp_path), Clock(), Sender()
    go(store, clock, AVAILABLE, sender)
    for _ in range(48):
        clock.tick()
        go(store, clock, POSSIBLY_AVAILABLE, sender, strength="strong")
    assert sender.kinds == [AVAILABLE]


def test_reminders_restart_after_new_episode(tmp_path):
    store, clock, sender = FileStateStore(tmp_path), Clock(), Sender()
    for s in [POSSIBLY_AVAILABLE, UNAVAILABLE, POSSIBLY_AVAILABLE]:
        go(store, clock, s, sender)
        clock.tick()
    assert [c["context"]["number"] for c in sender.calls] == [1, 1]


# ------------------------------------------------- owed (failed) restock mail ---

def test_failed_available_email_is_still_sent_while_site_is_blocked(tmp_path):
    store, clock = FileStateStore(tmp_path), Clock()
    assert go(store, clock, AVAILABLE, Sender(fail_kinds={AVAILABLE}),
              evidence=["productinfo=有货", "已成功加入购物袋"]) == EXIT_NOTIFY
    found_at = clock()
    clock.tick()
    s = Sender()
    go(store, clock, UNKNOWN, s, kind="blocked")      # next check blocked – still deliver
    assert s.kinds == [AVAILABLE]
    c = s.calls[0]
    assert c["context"]["owed"] is True and c["at"] == found_at
    assert c["evidence"] == ["productinfo=有货", "已成功加入购物袋"]
    assert "补发" in notifier.build_message(AVAILABLE, S, c["at"], c["evidence"], c["context"]).get_content()
    clock.tick()
    go(store, clock, UNKNOWN, s)
    assert s.kinds == [AVAILABLE]                     # exactly once


def test_owed_email_expires(tmp_path):
    store, clock = FileStateStore(tmp_path), Clock()
    go(store, clock, AVAILABLE, Sender(fail_kinds={AVAILABLE}))
    clock.tick(60 * 25)
    s = Sender()
    go(store, clock, UNKNOWN, s)
    assert AVAILABLE not in s.kinds


# ------------------------------------------------ 3. claim → send → confirm ---

class FlakyStore(FileStateStore):
    """Fails saves with ERROR according to a schedule (list of booleans, True=fail)."""

    def __init__(self, path, fail_plan):
        super().__init__(path)
        self.plan = list(fail_plan)

    def save(self, *a, **k):
        if self.plan and self.plan.pop(0):
            return SaveResult.ERROR
        return super().save(*a, **k)


def test_claim_cannot_be_saved_still_sends_restock(tmp_path):
    store, clock, sender = FlakyStore(tmp_path, [True] * 100), Clock(), Sender()
    code = go(store, clock, AVAILABLE, sender)
    assert sender.kinds == [AVAILABLE]       # sent although nothing could be recorded
    assert code == EXIT_STATE


def test_claim_save_transient_error_is_retried(tmp_path):
    store, clock, sender = FlakyStore(tmp_path, [True, True]), Clock(), Sender()
    assert go(store, clock, AVAILABLE, sender) == EXIT_OK
    assert store.load()[0]["episode"]["available_sent_at"]


def test_state_unwritable_does_not_spam_health_mails(tmp_path):
    store, clock, sender = FlakyStore(tmp_path, [True] * 1000), Clock(), Sender()
    for _ in range(10):
        go(store, clock, UNKNOWN, sender)
        clock.tick()
    assert sender.kinds == []


def test_sent_but_confirm_not_saved_documented_duplicate_with_same_message_id(tmp_path):
    # claim saved OK, send OK, then every confirm save fails → next run resends.
    store, clock, s1 = FlakyStore(tmp_path, [False] + [True] * 6), Clock(), Sender()
    assert go(store, clock, AVAILABLE, s1) == EXIT_STATE
    assert store.load()[0]["pending"]                  # claim still recorded
    clock.tick(5)
    s2 = Sender()
    go(store, clock, AVAILABLE, s2)
    assert s2.kinds == []                              # fresh claim: no immediate duplicate
    clock.tick(60)
    s3 = Sender()
    go(store, clock, AVAILABLE, s3)
    assert s3.kinds == [AVAILABLE]                     # stale claim → resend (possible duplicate)
    k1, k3 = s1.calls[0]["context"]["dedupe_key"], s3.calls[0]["context"]["dedupe_key"]
    assert k1 == k3                                    # same Message-ID → Gmail can drop it
    m1 = notifier.build_message(AVAILABLE, S, T0, [], s1.calls[0]["context"])
    m3 = notifier.build_message(AVAILABLE, S, T0, [], s3.calls[0]["context"])
    assert m1["Message-ID"] == m3["Message-ID"]


def test_process_killed_after_claim_is_recovered(tmp_path):
    store, clock = FileStateStore(tmp_path), Clock()

    def killed(*a, **k):
        raise SystemExit("runner killed")

    with pytest.raises(SystemExit):
        go(store, clock, AVAILABLE, killed)
    clock.tick(60)
    s = Sender()
    go(store, clock, AVAILABLE, s)
    assert s.kinds == [AVAILABLE]


def test_different_notifications_get_different_message_ids(tmp_path):
    store, clock, sender = FileStateStore(tmp_path), Clock(), Sender()
    for st in [AVAILABLE, UNAVAILABLE, AVAILABLE]:
        go(store, clock, st, sender)
        clock.tick()
    keys = [c["context"]["dedupe_key"] for c in sender.calls]
    assert len(keys) == 2 and keys[0] != keys[1]


def test_skip_never_happens_with_owed_or_positive_state(tmp_path):
    s180 = replace(S, check_interval_minutes=180)
    store, clock = FileStateStore(tmp_path), Clock()
    go(store, clock, AVAILABLE, Sender(fail_kinds={AVAILABLE}), settings=s180)
    clock.tick(60)
    calls = []

    def checker(s):
        calls.append(1)
        return CheckResult(AVAILABLE, [], clock().isoformat(), {})

    run(store, s180, checker=checker, sender=Sender(), now_fn=clock, sleep=NOSLEEP)
    assert calls == [1]


def test_schema1_state_is_migrated():
    st, ok = normalize({"schema": 1, "last_status": "AVAILABLE",
                        "notified": {"AVAILABLE": "2026-11-19T12:00:00+00:00", "POSSIBLY_AVAILABLE": None},
                        "pending": None})
    assert ok and st["episode"]["available_sent_at"] == "2026-11-19T12:00:00+00:00"


def test_healthcheck_ping(monkeypatch, tmp_path):
    pinged = []

    class R:
        def read(self):
            return b""

    monkeypatch.setattr("monitor.run.urllib.request.urlopen", lambda u, timeout: pinged.append(u) or R())
    s = replace(S, healthcheck_url="https://hc-ping.example/abc")
    store, clock = FileStateStore(tmp_path), Clock()
    go(store, clock, UNAVAILABLE, Sender(), settings=s)
    for _ in range(3):
        clock.tick()
        go(store, clock, UNKNOWN, Sender(), settings=s)
    assert pinged[0] == "https://hc-ping.example/abc"
    assert pinged[-1] == "https://hc-ping.example/abc/fail"
    assert "abc" not in status_json(tmp_path).__repr__()


# ------------------------------------------------- git store: real git errors ---

def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_pair(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))       # no user/global git config
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (tmp_path / "home").mkdir()
    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "monitor-data", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=seed)
    _git("push", "-q", str(remote), "monitor-data", cwd=seed)
    stores = []
    for name in ("a", "b"):
        d = tmp_path / name
        _git("clone", "-q", "-b", "monitor-data", str(remote), str(d), cwd=tmp_path)
        _git("config", "user.name", "bot", cwd=d)
        _git("config", "user.email", "bot@example.com", cwd=d)
        stores.append(GitBranchStateStore(d))
    return stores


def test_git_confirm_survives_push_conflict(git_pair):
    a, b = git_pair
    clock = Clock()

    class InterferingSender(Sender):
        def __call__(self, kind, *args, **kw):
            super().__call__(kind, *args, **kw)
            b._git("fetch", "-q", "origin")  # another, up-to-date writer pushes while we send
            b._git("reset", "-q", "--hard", "origin/monitor-data")
            st, v = b.load()
            st["email"]["last_test_ok_at"] = "2026-11-19T00:00:00+00:00"
            assert b.save(st, v) is SaveResult.OK

    s = InterferingSender()
    assert go(a, clock, AVAILABLE, s) == EXIT_OK
    st, _ = a.load()
    assert st["episode"]["available_sent_at"] and st["pending"] == {}
    assert st["email"]["last_test_ok_at"]  # other writer's change kept
    s2 = Sender()
    clock.tick()
    go(b, clock, AVAILABLE, s2)            # b must not send again after pulling a's state
    b._git("fetch", "-q", "origin")
    b._git("reset", "-q", "--hard", "origin/monitor-data")
    go(b, clock, AVAILABLE, s2)
    assert s.kinds == [AVAILABLE] and s2.kinds == []


def test_git_push_error_is_not_treated_as_conflict(git_pair, tmp_path):
    a, _ = git_pair
    st, v = a.load()
    a._git("remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    st["last_status"] = UNAVAILABLE
    assert a.save(st, v) is SaveResult.ERROR
    assert a.load()[1] == v                # local commit undone
