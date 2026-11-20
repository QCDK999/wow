"""Static checks of the GitHub workflows + real runs of the data-branch script.

There is no actionlint in this environment, so these tests check what matters
for notification reliability explicitly.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"


def load(name):
    data = yaml.safe_load((WF / name).read_text())
    data["on"] = data.pop(True, data.get("on"))  # PyYAML parses the key `on` as True
    return data


@pytest.fixture(scope="module")
def monitor():
    return load("monitor.yml")


def steps(wf):
    return wf["jobs"]["monitor"]["steps"]


def step(wf, prefix):
    (s,) = [s for s in steps(wf) if s.get("name", "").startswith(prefix)]
    return s


def test_triggers_and_concurrency(monitor):
    assert re.fullmatch(r"\d{1,2} \* \* \* \*", monitor["on"]["schedule"][0]["cron"])  # hourly
    assert "workflow_dispatch" in monitor["on"]
    assert monitor["on"]["workflow_dispatch"]["inputs"]["mode"]["options"] == ["check", "test-email"]
    assert monitor["concurrency"] == {"group": "vca-monitor", "cancel-in-progress": False}
    assert monitor["permissions"] == {"contents": "write", "actions": "write"}
    assert monitor["jobs"]["monitor"]["timeout-minutes"] <= 20


def test_all_settings_and_secrets_wired(monitor):
    env = monitor["jobs"]["monitor"]["env"]
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM", "NOTIFY_TO",
              "HEALTHCHECK_URL"):
        assert env[k] == "${{ secrets.%s }}" % k
    for k in ("CHECK_INTERVAL_MINUTES", "MONITOR_PAUSED", "VERIFY_CART", "NOTIFY_POSSIBLY",
              "POSSIBLY_MAX_EMAILS", "HEALTH_ALERT_AFTER", "HEALTH_REMIND_HOURS"):
        assert env[k].startswith("${{ vars.%s ||" % k)
    # every env var the Python code reads is provided by the workflow
    code = "".join(p.read_text() for p in (ROOT / "monitor").glob("*.py"))
    read = set(re.findall(r'_(?:bool|int)\("([A-Z_]+)"', code)) | set(
        re.findall(r'os\.environ\.get\("([A-Z_]+)"', code))
    local_only = {"PAGE_TIMEOUT_MS", "SETTLE_MS", "MAX_ATTEMPTS", "HEADLESS", "BROWSER_CHANNEL",
                  "SMTP_SECURITY", "GITHUB_RUN_ID", "GITHUB_SERVER_URL", "GITHUB_REPOSITORY"}
    assert read - local_only <= set(env), read - local_only - set(env)


def test_no_untrusted_expression_inside_run_scripts(monitor):
    for s in steps(monitor):
        if "run" in s:
            assert "${{" not in s["run"], f"step {s.get('name')} interpolates an expression into a script"


def test_step_order_and_failure_visibility(monitor):
    names = [s.get("name", s.get("uses")) for s in steps(monitor)]
    assert names.index("Check out monitor-data branch") < names.index("Check stock")
    assert step(monitor, "Show result")["if"].startswith("always()")
    assert step(monitor, "Upload raw check result")["if"].startswith("always()")
    keep = step(monitor, "Keep schedule alive")
    assert keep["if"] == "always() && github.event_name == 'schedule'" and keep["continue-on-error"] is True
    # The check step must NOT swallow failures (no `|| true`, no continue-on-error)
    chk = step(monitor, "Check stock")
    assert "|| true" not in chk["run"] and not chk.get("continue-on-error")


def test_run_scripts_are_valid_bash(monitor):
    for s in steps(monitor):
        if "run" in s:
            r = subprocess.run(["bash", "-n"], input=s["run"], text=True, capture_output=True)
            assert r.returncode == 0, (s.get("name"), r.stderr)
    r = subprocess.run(["bash", "-n", str(ROOT / "scripts/prepare_data_branch.sh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_summary_step_runs(monitor, tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "check-result.json").write_text(json.dumps({
        "status": "UNKNOWN", "evidence": ["访问被拦截"], "checked_at": "x", "strength": None,
        "unknown_kind": "blocked", "signals": {"http_status": 403, "challenge": True,
                                               "challenge_reason": "HTTP 403", "human_preloaded": True}}))
    (tmp_path / "data/status.json").write_text(json.dumps({"monitoring": {
        "state": "error", "message": "连续 3 次无法判断库存", "consecutive_unknown": 3}}))
    summary = tmp_path / "summary.md"
    env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary), "MODE": "check"}
    r = subprocess.run(["bash", "-e", "-c", step(monitor, "Show result")["run"]], cwd=tmp_path, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = summary.read_text()
    assert "**UNKNOWN**" in out and "unknown_kind=blocked" in out and "consecutive UNKNOWN: 3" in out


def test_tests_workflow_builds_frontend():
    t = load("tests.yml")
    runs = [s.get("run", "") for s in t["jobs"]["frontend"]["steps"]]
    assert "npm run build" in runs and "npm test" in runs


# ---------------------------------------------------- prepare_data_branch.sh ---

def _git(*args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    work = tmp_path / "work"
    shutil.copytree(ROOT / "scripts", work / "scripts")
    _git("init", "-q", "-b", "main", cwd=work)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", cwd=work)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--no-gpg-sign", "-m", "code", cwd=work)
    _git("remote", "add", "origin", str(remote), cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    return tmp_path, remote


def _runner(tmp_path, remote, name):
    d = tmp_path / name
    _git("clone", "-q", "--depth", "1", f"file://{remote}", str(d), cwd=tmp_path)
    return d


def _prepare(d):
    return subprocess.run(["bash", "scripts/prepare_data_branch.sh"], cwd=d, capture_output=True, text=True)


def test_data_branch_created_then_reused(repo):
    tmp_path, remote = repo
    r1 = _runner(tmp_path, remote, "r1")
    out = _prepare(r1)
    assert out.returncode == 0, out.stderr
    assert "creating it" in out.stdout
    assert _git("ls-tree", "--name-only", "monitor-data", cwd=remote).stdout.split() == ["README.md"]  # no code
    r2 = _runner(tmp_path, remote, "r2")
    out = _prepare(r2)
    assert out.returncode == 0, out.stderr and "creating" not in out.stdout
    (r2 / "data/state.json").write_text("{}")
    _git("-C", "data", "add", "-A", cwd=r2)
    _git("-C", "data", "commit", "-q", "--no-gpg-sign", "-m", "x", cwd=r2)
    assert _git("-C", "data", "push", "-q", "origin", "HEAD:monitor-data", cwd=r2).returncode == 0


def test_data_branch_without_write_access_fails_loudly(repo):
    tmp_path, remote = repo
    _prepare(_runner(tmp_path, remote, "r1"))
    # Simulate a read-only token: a pre-receive hook that rejects every push.
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'permission denied' >&2\nexit 1\n")
    hook.chmod(0o755)
    r2 = _runner(tmp_path, remote, "r2")
    out = _prepare(r2)
    # Dry-run pushes don't run hooks, so for an existing branch the real check
    # happens on the first state save (SaveResult.ERROR → exit 1, see
    # test_git_push_error_is_not_treated_as_conflict). Creating the branch,
    # however, must fail loudly:
    shutil.rmtree(tmp_path / "remote2.git", ignore_errors=True)
    remote2 = tmp_path / "remote2.git"
    _git("clone", "-q", "--bare", str(remote), str(remote2), cwd=tmp_path)
    _git("--git-dir", str(remote2), "branch", "-D", "monitor-data", cwd=tmp_path)
    (remote2 / "hooks" / "pre-receive").write_text("#!/bin/sh\nexit 1\n")
    (remote2 / "hooks" / "pre-receive").chmod(0o755)
    r3 = _runner(tmp_path, remote2, "r3")
    out = _prepare(r3)
    assert out.returncode == 1
    assert "Workflow permissions" in out.stderr


def test_data_branch_dry_run_detects_missing_remote(repo):
    tmp_path, remote = repo
    _prepare(_runner(tmp_path, remote, "r1"))
    r2 = _runner(tmp_path, remote, "r2")
    _git("config", "remote.origin.pushurl", str(tmp_path / "nope.git"), cwd=r2)
    out = _prepare(r2)
    assert out.returncode == 1 and "Cannot push" in out.stderr
