import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone

import pytest

from monitor import config, notifier
from monitor.check_stock import AVAILABLE, POSSIBLY_AVAILABLE, UNAVAILABLE, UNKNOWN, CheckResult
from monitor.run import build_status, run, send_test_email
from monitor.state import FileStateStore, GitBranchStateStore, decide, default_state

T0 = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
SETTINGS = config.Settings(smtp_host="smtp.example.com", smtp_username="u", smtp_password="p",
                           notify_to="me@example.com")


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def tick(self, minutes=60):  # noqa: D401
        self.t += timedelta(minutes=minutes)


class Sender:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, kind, settings, checked_at, evidence, **kw):
        if self.fail:
            raise notifier.NotificationError("SMTP down")
        self.sent.append(kind)


def checker_for(clock, status):
    def checker(settings):
        return CheckResult(status=status, evidence=[f"fake {status}"],
                           checked_at=clock().isoformat(), signals={})
    return checker


def sequence(tmp_path, statuses, sender=None, settings=SETTINGS):
    store, clock, sender = FileStateStore(tmp_path), Clock(), sender or Sender()
    codes = []
    for s in statuses:
        codes.append(run(store, settings, checker=checker_for(clock, s), sender=sender, now_fn=clock,
                         sleep=lambda x: None))
        clock.tick()
    return store, sender, codes


# 10. consecutive AVAILABLE → one email
def test_10_repeated_available_sends_once(tmp_path):
    _, sender, _ = sequence(tmp_path, [AVAILABLE, AVAILABLE, AVAILABLE])
    assert sender.sent == [AVAILABLE]


# 11. UNAVAILABLE → AVAILABLE, and a second restock after going out of stock again
def test_11_unavailable_to_available(tmp_path):
    _, sender, _ = sequence(tmp_path, [UNAVAILABLE, AVAILABLE, UNAVAILABLE, UNAVAILABLE, AVAILABLE])
    assert sender.sent == [AVAILABLE, AVAILABLE]


# 12. UNKNOWN → AVAILABLE sends; UNKNOWN never resets the flag
def test_12_unknown_to_available_and_unknown_does_not_reset(tmp_path):
    _, sender, _ = sequence(tmp_path, [UNKNOWN, AVAILABLE, UNKNOWN, AVAILABLE])
    assert sender.sent == [AVAILABLE]


# 13. first ever run already AVAILABLE → exactly one email
def test_13_first_run_available(tmp_path):
    store, sender, codes = sequence(tmp_path, [AVAILABLE])
    assert sender.sent == [AVAILABLE] and codes == [0]
    state, _ = store.load()
    assert state["episode"]["available_sent_at"] and state["pending"] == {}


def test_possibly_then_available_sends_both(tmp_path):
    _, sender, _ = sequence(tmp_path, [UNAVAILABLE, POSSIBLY_AVAILABLE, POSSIBLY_AVAILABLE, AVAILABLE,
                                       POSSIBLY_AVAILABLE, AVAILABLE])
    assert sender.sent == [POSSIBLY_AVAILABLE, AVAILABLE]


def test_possibly_mails_can_be_disabled(tmp_path):
    from dataclasses import replace
    s = replace(SETTINGS, notify_possibly=False)
    _, sender, _ = sequence(tmp_path, [POSSIBLY_AVAILABLE, AVAILABLE], settings=s)
    assert sender.sent == [AVAILABLE]


# 9. email failure → not marked sent, retried next run, run exits 2
def test_9_email_failure_is_retried(tmp_path):
    store, clock = FileStateStore(tmp_path), Clock()
    bad = Sender(fail=True)
    assert run(store, SETTINGS, checker=checker_for(clock, AVAILABLE), sender=bad, now_fn=clock) == 2
    state, _ = store.load()
    assert state["episode"]["available_sent_at"] is None and state["pending"] == {}
    assert state["email"]["last_error_at"]
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["monitoring"]["state"] == "error"
    clock.tick()
    good = Sender()
    assert run(store, SETTINGS, checker=checker_for(clock, AVAILABLE), sender=good, now_fn=clock) == 0
    assert good.sent == [AVAILABLE]
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["monitoring"]["state"] == "active"


def test_unconfigured_email_fails_loudly(tmp_path):
    store, clock = FileStateStore(tmp_path), Clock()
    s = config.Settings()
    code = run(store, s, checker=checker_for(clock, AVAILABLE), sender=notifier.send, now_fn=clock)
    assert code == 2
    assert json.loads((tmp_path / "status.json").read_text())["notifications"]["configured"] is False


# 14. two runs at the same moment → exactly one email
def test_14_concurrent_runs_send_once(tmp_path):
    store = FileStateStore(tmp_path)
    clock = Clock()
    barrier = threading.Barrier(2)
    sent, lock = [], threading.Lock()

    def slow_checker(settings):
        barrier.wait()  # both runs finish checking at the same time
        return CheckResult(status=AVAILABLE, evidence=[], checked_at=clock().isoformat(), signals={})

    def sender(kind, *a, **k):
        with lock:
            sent.append(kind)

    codes = []
    threads = [threading.Thread(target=lambda: codes.append(
        run(store, SETTINGS, checker=slow_checker, sender=sender, now_fn=clock, run_id=rid)))
        for rid in ("run-a", "run-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sent == [AVAILABLE]
    assert codes == [0, 0]


def test_crash_between_claim_and_send_is_recovered(tmp_path):
    store, clock = FileStateStore(tmp_path), Clock()
    state, ver = store.load()
    d = decide(state, AVAILABLE, clock(), "crashed-run")
    store.save(d.state, ver)  # claimed, then the runner died
    clock.tick(5)
    s1 = Sender()
    run(store, SETTINGS, checker=checker_for(clock, AVAILABLE), sender=s1, now_fn=clock, force=True)
    assert s1.sent == []  # fresh claim: someone may still be sending
    clock.tick(30)
    s2 = Sender()
    run(store, SETTINGS, checker=checker_for(clock, AVAILABLE), sender=s2, now_fn=clock, force=True)
    assert s2.sent == [AVAILABLE]


# 15. missing or corrupt state file
def test_15_missing_state_file(tmp_path):
    store = FileStateStore(tmp_path / "new")
    state, version = store.load()
    assert state == default_state() and version == "absent"


@pytest.mark.parametrize("content", ["{not json", "[]", '{"schema": 99}', '{"schema": 1, "notified": 5}', ""])
def test_15_corrupt_state_file(tmp_path, content):
    (tmp_path / "state.json").write_text(content)
    store, clock, sender = FileStateStore(tmp_path), Clock(), Sender()
    assert run(store, SETTINGS, checker=checker_for(clock, AVAILABLE), sender=sender, now_fn=clock) == 0
    assert sender.sent == [AVAILABLE]
    json.loads((tmp_path / "state.json").read_text())  # repaired


def test_interval_skip_does_not_touch_site(tmp_path):
    store, clock, sender = FileStateStore(tmp_path), Clock(), Sender()
    calls = []

    def checker(s):
        calls.append(1)
        return CheckResult(UNAVAILABLE, [], clock().isoformat(), {})

    from dataclasses import replace
    s = replace(SETTINGS, check_interval_minutes=180)
    run(store, s, checker=checker, sender=sender, now_fn=clock)
    clock.tick(60)
    run(store, s, checker=checker, sender=sender, now_fn=clock)
    assert len(calls) == 1
    run(store, s, checker=checker, sender=sender, now_fn=clock, force=True)
    assert len(calls) == 2


def test_paused_publishes_status_without_checking(tmp_path):
    from dataclasses import replace
    store, clock = FileStateStore(tmp_path), Clock()
    called = []
    run(store, replace(SETTINGS, monitor_paused=True), checker=lambda s: called.append(1), now_fn=clock)
    assert not called
    assert json.loads((tmp_path / "status.json").read_text())["monitoring"]["state"] == "paused"


def test_status_json_has_no_secrets(tmp_path):
    sequence(tmp_path, [AVAILABLE])
    text = (tmp_path / "status.json").read_text()
    for secret in ("smtp.example.com", "me@example.com", '"p"'):
        assert secret not in text
    st = json.loads(text)
    assert st["stock"]["status"] == AVAILABLE and st["product"]["ref"] == "VCARD34700"
    assert st["monitoring"]["stale_after"] > st["generated_at"]


def test_test_email_recorded(tmp_path):
    store = FileStateStore(tmp_path)
    assert send_test_email(store, SETTINGS, sender=Sender()) == 0
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["notifications"]["last_test_ok_at"]
    assert send_test_email(store, SETTINGS, sender=Sender(fail=True)) == 1
    assert json.loads((tmp_path / "status.json").read_text())["notifications"]["last_test_error_at"]


def test_unknown_status_shows_monitoring_error():
    st = default_state()
    st["last_status"], st["last_check_at"] = UNKNOWN, T0.isoformat()
    out = build_status(st, SETTINGS, T0)
    assert out["monitoring"]["state"] == "error" and out["stock"]["label"] == "Unknown"


# Git-backed store: push conflict is detected and resolved without double sending
def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_pair(tmp_path):
    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "monitor-data", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=seed)
    _git("push", "-q", str(remote), "monitor-data", cwd=seed)
    clones = []
    for name in ("a", "b"):
        d = tmp_path / name
        _git("clone", "-q", "-b", "monitor-data", str(remote), str(d), cwd=tmp_path)
        _git("config", "user.name", "bot", cwd=d)
        _git("config", "user.email", "bot@example.com", cwd=d)
        clones.append(GitBranchStateStore(d))
    return clones


def test_14_git_store_conflict(git_pair):
    a, b = git_pair
    clock = Clock()
    sent = []
    sender = lambda kind, *x, **k: sent.append(kind)  # noqa: E731
    sa, va = a.load()
    sb, vb = b.load()
    da = decide(sa, AVAILABLE, clock(), "a")
    db = decide(sb, AVAILABLE, clock(), "b")
    assert [x.kind for x in da.sends] == [x.kind for x in db.sends] == [AVAILABLE]  # both want to send …
    assert a.save(da.state, va)              # … a wins the push
    assert not b.save(db.state, vb)          # … b is rejected and resets to remote
    sb2, vb2 = b.load()
    assert decide(sb2, AVAILABLE, clock(), "b").sends == []  # sees a's claim
    # full run on b afterwards does not send either
    run(b, SETTINGS, checker=checker_for(clock, AVAILABLE), sender=sender, now_fn=clock, force=True)
    assert sent == []
