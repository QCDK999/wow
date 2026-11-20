"""Persistent state, notification de-duplication and monitor health.

State lives in ``state.json`` on a dedicated git branch (``monitor-data``) so it
survives between GitHub Actions runs. Writes use optimistic concurrency: a save
only succeeds if nobody else saved since we loaded (file hash / git push
fast-forward).

Notifications use *claim → send → confirm*:

1. claim:   write ``pending[kind]``. If another run already claimed or sent,
            the save CONFLICTs and we re-decide from the fresh state.
            If the save fails for another reason (permissions, network) we
            send anyway – a possible duplicate is better than a missed restock.
2. send:    send the email.
3. confirm: success → record it; failure → drop the claim so the next run retries.

Duplicates that cannot be fully prevented (documented in README):
* the process dies after the SMTP server accepted the mail but before confirm
  was saved → the claim goes stale after ``PENDING_STALE_MINUTES`` and the
  next run sends again;
* the claim cannot be written at all (e.g. no push permission) → we send
  every run until the state can be written again.

Policy in one place (``Policy``):
* AVAILABLE: one email per restock episode (episode ends on UNAVAILABLE).
* POSSIBLY_AVAILABLE: first email, then limited reminders while it persists
  (default after 2 h, 6 h, then every 24 h, at most 6 emails per episode), and
  an immediate escalation email when the evidence becomes "strong" (every page
  signal says in stock, only the bag check fails).
* A restock email that could not be delivered is still owed when later checks
  are UNKNOWN (site blocked): it is retried for ``owed_hours`` using the
  evidence of the original detection.
* Health: ``alert_after`` consecutive UNKNOWN results → one "monitor down"
  email, repeated at most every ``health_remind_hours``; one "recovered"
  email when a check succeeds again. UNKNOWN never resets restock state.
"""

from __future__ import annotations

import copy
import enum
import fcntl
import hashlib
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .check_stock import AVAILABLE, POSSIBLY_AVAILABLE, UNAVAILABLE, UNKNOWN

log = logging.getLogger("vca.state")

SCHEMA = 2
PENDING_STALE_MINUTES = 15

HEALTH_DOWN = "HEALTH_DOWN"
HEALTH_RECOVERED = "HEALTH_RECOVERED"
STOCK_KINDS = (AVAILABLE, POSSIBLY_AVAILABLE)
ALL_KINDS = (AVAILABLE, POSSIBLY_AVAILABLE, HEALTH_DOWN, HEALTH_RECOVERED)


@dataclass(frozen=True)
class Policy:
    alert_after: int = 3                      # consecutive UNKNOWN before alerting
    health_remind_hours: float = 24
    possibly_gaps_hours: tuple[float, ...] = (2, 6, 24)  # then the last value repeats
    possibly_max_emails: int = 6
    owed_hours: float = 24
    allow_possibly: bool = True


def default_episode() -> dict[str, Any]:
    return {"available_sent_at": None, "possibly_sent": [], "possibly_strength": None,
            "first_seen_at": None, "last_positive": None}


def default_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "last_status": None,
        "last_check_at": None,
        "last_ok_check_at": None,
        "last_evidence": [],
        "episode": default_episode(),
        "pending": {},
        "health": {"consecutive_unknown": 0, "first_unknown_at": None, "last_reason": None,
                   "last_kind": None, "alert_active": False, "alert_sent_at": None},
        "email": {"last_sent_at": None, "last_kind": None, "last_error_at": None,
                  "last_error": None, "last_test_ok_at": None, "last_test_error_at": None},
    }


def _s(v: Any) -> str | None:
    return v if isinstance(v, str) else None


def normalize(raw: Any) -> tuple[dict[str, Any], bool]:
    """Return (state, was_valid). Corrupt input → fresh state. Schema 1 is migrated."""
    base = default_state()
    if raw is None:
        return base, True
    if not isinstance(raw, dict) or raw.get("schema") not in (1, SCHEMA):
        return base, False
    try:
        st = copy.deepcopy(base)
        if raw.get("last_status") in (AVAILABLE, POSSIBLY_AVAILABLE, UNAVAILABLE, UNKNOWN):
            st["last_status"] = raw["last_status"]
        for k in ("last_check_at", "last_ok_check_at"):
            st[k] = _s(raw.get(k))
        ev = raw.get("last_evidence")
        st["last_evidence"] = [str(x)[:300] for x in ev][:20] if isinstance(ev, list) else []
        e = raw.get("email") if isinstance(raw.get("email"), dict) else {}
        for k in st["email"]:
            st["email"][k] = _s(e.get(k))

        if raw["schema"] == 1:  # migrate: notified{} → episode
            n = raw.get("notified") if isinstance(raw.get("notified"), dict) else {}
            st["episode"]["available_sent_at"] = _s(n.get(AVAILABLE))
            if _s(n.get(POSSIBLY_AVAILABLE)):
                st["episode"]["possibly_sent"] = [n[POSSIBLY_AVAILABLE]]
            return st, True

        ep = raw.get("episode") if isinstance(raw.get("episode"), dict) else {}
        st["episode"]["available_sent_at"] = _s(ep.get("available_sent_at"))
        st["episode"]["first_seen_at"] = _s(ep.get("first_seen_at"))
        if ep.get("possibly_strength") in ("weak", "strong"):
            st["episode"]["possibly_strength"] = ep["possibly_strength"]
        ps = ep.get("possibly_sent")
        st["episode"]["possibly_sent"] = [x for x in ps if isinstance(x, str)][:50] if isinstance(ps, list) else []
        lp = ep.get("last_positive")
        if isinstance(lp, dict) and lp.get("status") in STOCK_KINDS and _s(lp.get("at")):
            st["episode"]["last_positive"] = {
                "status": lp["status"], "at": lp["at"], "strength": lp.get("strength"),
                "evidence": [str(x)[:300] for x in lp.get("evidence", []) if isinstance(x, str)][:20]}
        pend = raw.get("pending") if isinstance(raw.get("pending"), dict) else {}
        for kind, p in pend.items():
            if kind in ALL_KINDS and isinstance(p, dict) and _s(p.get("claimed_at")):
                st["pending"][kind] = {"claimed_at": p["claimed_at"], "run_id": str(p.get("run_id", ""))}
        h = raw.get("health") if isinstance(raw.get("health"), dict) else {}
        c = h.get("consecutive_unknown")
        st["health"]["consecutive_unknown"] = c if isinstance(c, int) and c >= 0 else 0
        for k in ("first_unknown_at", "last_reason", "last_kind", "alert_sent_at"):
            st["health"][k] = _s(h.get(k))
        st["health"]["alert_active"] = h.get("alert_active") is True
        return st, True
    except Exception:
        return base, False


# ---------------------------------------------------------------------------
# Decision logic (pure)
# ---------------------------------------------------------------------------

@dataclass
class Send:
    kind: str
    checked_at: datetime
    evidence: list[str]
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    state: dict[str, Any]          # state to persist *before* sending (contains claims)
    sends: list[Send]

    @property
    def stock_sends(self) -> list[Send]:
        return [s for s in self.sends if s.kind in STOCK_KINDS]


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def is_degraded(state: dict[str, Any], policy: Policy) -> bool:
    return state["health"]["consecutive_unknown"] >= policy.alert_after


def decide(state: dict[str, Any], status: str, now: datetime, run_id: str,
           evidence: list[str] | None = None, *, policy: Policy = Policy(),
           strength: str | None = None, unknown_kind: str | None = None) -> Decision:
    """Record a check result and decide which emails to send (and claim them)."""
    st = copy.deepcopy(state)
    evidence = list(evidence or [])[:20]
    st["last_status"], st["last_check_at"], st["last_evidence"] = status, _iso(now), evidence
    ep, h, pending = st["episode"], st["health"], st["pending"]
    sends: list[Send] = []

    # Drop claims left behind by runs that died (treated as "not sent").
    for kind, p in list(pending.items()):
        claimed = _parse(p.get("claimed_at"))
        if claimed is None or now - claimed > timedelta(minutes=PENDING_STALE_MINUTES):
            log.warning("dropping stale %s claim from run %s – will retry", kind, p.get("run_id"))
            del pending[kind]

    def claim(kind: str, at: datetime, ev: list[str], key: str, **ctx: Any) -> None:
        pending[kind] = {"claimed_at": _iso(now), "run_id": run_id}
        ctx["dedupe_key"] = f"{kind}|{key}"   # same logical email → same Message-ID
        sends.append(Send(kind, at, ev, ctx))

    # ---- health -------------------------------------------------------------
    if status == UNKNOWN:
        h["consecutive_unknown"] += 1
        h["first_unknown_at"] = h["first_unknown_at"] or _iso(now)
        h["last_reason"] = "; ".join(evidence)[:300] or None
        h["last_kind"] = unknown_kind or "error"
        last_alert = _parse(h["alert_sent_at"])
        due = (not h["alert_active"] or last_alert is None
               or now - last_alert >= timedelta(hours=policy.health_remind_hours))
        if h["consecutive_unknown"] >= policy.alert_after and due and HEALTH_DOWN not in pending:
            claim(HEALTH_DOWN, now, evidence, f"{h['first_unknown_at']}|{h['alert_sent_at']}",
                  health=copy.deepcopy(h),
                  last_ok_check_at=st["last_ok_check_at"])
    else:
        if h["alert_active"] and HEALTH_RECOVERED not in pending:
            claim(HEALTH_RECOVERED, now, evidence, str(h["first_unknown_at"]), down_since=h["first_unknown_at"])
            h["alert_active"] = False  # recovery mail is best effort, never repeated
        h.update(consecutive_unknown=0, first_unknown_at=None, last_reason=None, last_kind=None)
        st["last_ok_check_at"] = _iso(now)

    # ---- restock episode ----------------------------------------------------
    if status == UNAVAILABLE:
        st["episode"] = default_episode()
        for k in STOCK_KINDS:
            pending.pop(k, None)
        return Decision(st, sends)

    if status in STOCK_KINDS:
        ep["first_seen_at"] = ep["first_seen_at"] or _iso(now)
        ep["last_positive"] = {"status": status, "at": _iso(now), "strength": strength,
                               "evidence": evidence}

    # What are we allowed to act on? Fresh evidence, or – while the site can't be
    # read – an earlier positive result whose email is still owed.
    owed = False
    effective, ev_at, ev_list, ev_strength = status, now, evidence, strength
    lp = ep["last_positive"]
    if status == UNKNOWN and lp:
        lp_at = _parse(lp["at"])
        if lp_at and now - lp_at <= timedelta(hours=policy.owed_hours):
            owed = True
            effective, ev_at, ev_list, ev_strength = lp["status"], lp_at, lp["evidence"], lp.get("strength")

    if effective == AVAILABLE:
        if not ep["available_sent_at"] and AVAILABLE not in pending:
            claim(AVAILABLE, ev_at, ev_list, str(ep["first_seen_at"]), owed=owed)
    elif (effective == POSSIBLY_AVAILABLE and policy.allow_possibly
          and not ep["available_sent_at"] and POSSIBLY_AVAILABLE not in pending):
        sent = ep["possibly_sent"]
        if not sent:
            claim(POSSIBLY_AVAILABLE, ev_at, ev_list, f"{ep['first_seen_at']}|1", owed=owed, number=1,
                  strength=ev_strength, reason="first")
        elif not owed and len(sent) < policy.possibly_max_emails:
            # Reminders and escalation only on fresh evidence.
            last = _parse(sent[-1])
            gaps = policy.possibly_gaps_hours
            gap = gaps[min(len(sent) - 1, len(gaps) - 1)]
            if strength == "strong" and ep["possibly_strength"] != "strong":
                claim(POSSIBLY_AVAILABLE, ev_at, ev_list, f"{ep['first_seen_at']}|{len(sent) + 1}",
                      number=len(sent) + 1, strength=strength, reason="escalation",
                      first_seen_at=ep["first_seen_at"])
            elif last is None or now - last >= timedelta(hours=gap):
                claim(POSSIBLY_AVAILABLE, ev_at, ev_list, f"{ep['first_seen_at']}|{len(sent) + 1}",
                      number=len(sent) + 1, strength=strength, reason="reminder",
                      first_seen_at=ep["first_seen_at"])
    return Decision(st, sends)


def confirm(state: dict[str, Any], send: Send, ok: bool, now: datetime, run_id: str,
            error: str | None = None) -> dict[str, Any]:
    st = copy.deepcopy(state)
    p = st["pending"].get(send.kind)
    if p and p.get("run_id") == run_id:
        del st["pending"][send.kind]
    ts = _iso(now)
    if ok:
        if send.kind == AVAILABLE:
            st["episode"]["available_sent_at"] = ts
        elif send.kind == POSSIBLY_AVAILABLE:
            st["episode"]["possibly_sent"].append(ts)
            if send.context.get("strength") == "strong":
                st["episode"]["possibly_strength"] = "strong"
            elif not st["episode"]["possibly_strength"]:
                st["episode"]["possibly_strength"] = "weak"
        elif send.kind == HEALTH_DOWN:
            st["health"]["alert_active"] = True
            st["health"]["alert_sent_at"] = ts
        st["email"]["last_sent_at"] = ts
        st["email"]["last_kind"] = send.kind
    else:
        if send.kind == HEALTH_DOWN:
            # Still mark the alert as active so the site/exit code reflect it;
            # alert_sent_at stays unset so the email is retried next run.
            st["health"]["alert_active"] = True
        st["email"]["last_error_at"] = ts
        st["email"]["last_error"] = (error or "send failed")[:200]
    return st


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------

class SaveResult(enum.Enum):
    OK = "ok"
    CONFLICT = "conflict"   # someone else wrote first – reload and re-decide
    ERROR = "error"         # could not write (permissions, network, disk)

    def __bool__(self) -> bool:  # truthy only on success (keeps `if store.save(...)` working)
        return self is SaveResult.OK


class StateStore(Protocol):
    def load(self) -> tuple[dict[str, Any], str]: ...
    def save(self, state: dict[str, Any], version: str,
             extra_files: dict[str, str] | None = None, message: str = "") -> SaveResult: ...


def _dump(state: dict[str, Any]) -> str:
    return json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


class FileStateStore:
    """Local file store with compare-and-swap (used locally and in tests)."""

    def __init__(self, directory: str | os.PathLike):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        self.lock = self.dir / ".state.lock"

    def _read(self) -> tuple[Any, str]:
        if not self.path.exists():
            return None, "absent"
        text = self.path.read_text(encoding="utf-8")
        version = hashlib.sha256(text.encode()).hexdigest()
        try:
            return json.loads(text), version
        except json.JSONDecodeError:
            return "corrupt", version

    def load(self) -> tuple[dict[str, Any], str]:
        raw, version = self._read()
        state, valid = normalize(raw)
        if not valid:
            log.warning("state.json corrupt or unknown schema – starting from a fresh state")
        return state, version

    def _write_files(self, state, extra_files) -> None:
        for name, content in (extra_files or {}).items():
            (self.dir / name).write_text(content, encoding="utf-8")
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(_dump(state), encoding="utf-8")
        os.replace(tmp, self.path)

    def save(self, state, version, extra_files=None, message="") -> SaveResult:
        try:
            with open(self.lock, "w") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                _, current = self._read()
                if current != version:
                    return SaveResult.CONFLICT
                self._write_files(state, extra_files)
                return SaveResult.OK
        except OSError as e:
            log.error("state write failed: %s", e)
            return SaveResult.ERROR


REJECTED_MARKERS = ("non-fast-forward", "fetch first", "[rejected]", "stale info", "cannot lock ref")


class GitBranchStateStore(FileStateStore):
    """A git work-tree checked out on the data branch. Version = HEAD commit.

    ``save`` commits and pushes. A non-fast-forward rejection means another run
    wrote first → reset to the remote and return CONFLICT. Any other failure
    (no permission, network) → undo the local commit and return ERROR.
    """

    def __init__(self, directory, branch: str = "monitor-data", remote: str = "origin"):
        super().__init__(directory)
        self.branch, self.remote = branch, remote

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.dir), *args], check=check,
                              capture_output=True, text=True, timeout=120)

    def _head(self) -> str:
        r = self._git("rev-parse", "HEAD", check=False)
        return r.stdout.strip() if r.returncode == 0 else "empty"

    def load(self):
        state, _ = super().load()
        return state, self._head()

    def save(self, state, version, extra_files=None, message="") -> SaveResult:
        try:
            if self._head() != version:
                return SaveResult.CONFLICT
            self._write_files(state, extra_files)
            self._git("add", "-A")
            if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
                return SaveResult.OK  # nothing changed
            self._git("commit", "-q", "--no-gpg-sign", "-m", message or "monitor: update state")
            push = self._git("push", "-q", "--porcelain", self.remote, f"HEAD:{self.branch}", check=False)
        except (subprocess.SubprocessError, OSError) as e:
            log.error("git commit failed: %s", e)
            self._undo(version)
            return SaveResult.ERROR
        if push.returncode == 0:
            return SaveResult.OK
        out = (push.stdout + push.stderr).lower()
        if any(m in out for m in REJECTED_MARKERS):
            log.warning("push rejected (another run wrote first)")
            self._git("fetch", "-q", self.remote, f"+refs/heads/{self.branch}:refs/remotes/{self.remote}/{self.branch}",
                      check=False)
            self._git("reset", "-q", "--hard", f"{self.remote}/{self.branch}", check=False)
            return SaveResult.CONFLICT
        log.error("git push failed (not a conflict): %s", (push.stderr or push.stdout).strip()[:300])
        self._undo(version)
        return SaveResult.ERROR

    def _undo(self, version: str) -> None:
        if version != "empty":
            self._git("reset", "-q", "--hard", version, check=False)
