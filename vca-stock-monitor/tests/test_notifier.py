import smtplib
from datetime import datetime, timezone

import pytest

from monitor import config, notifier
from monitor.check_stock import AVAILABLE, POSSIBLY_AVAILABLE

S = config.Settings(smtp_host="smtp.example.com", smtp_port=587, smtp_username="bot@example.com",
                    smtp_password="app-password-123", notify_to="me@example.com")
NOW = datetime(2026, 11, 19, 15, 30, tzinfo=timezone.utc)


class FakeSMTP:
    instances: list["FakeSMTP"] = []
    fail_times = 0
    auth_fail = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.sent, self.tls = host, port, [], False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        self.tls = True

    def login(self, u, p):
        if FakeSMTP.auth_fail:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def send_message(self, msg):
        if FakeSMTP.fail_times > 0:
            FakeSMTP.fail_times -= 1
            raise smtplib.SMTPServerDisconnected("boom")
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances, FakeSMTP.fail_times, FakeSMTP.auth_fail = [], 0, False
    monkeypatch.setattr(notifier.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(notifier.time, "sleep", lambda s: None)


def sent_messages():
    return [m for i in FakeSMTP.instances for m in i.sent]


def test_available_email_content():
    notifier.send(AVAILABLE, S, NOW, ["productinfo=有货", "已成功加入购物袋"])
    (msg,) = sent_messages()
    body = msg.get_content()
    assert msg["Subject"] == "🎉 VCA 五花手链可以在线购买了！"
    assert msg["To"] == "me@example.com"
    assert config.PRODUCT_URL in body and "VCARD34700" in body and "AVAILABLE" in body
    assert "10:30:00 EST" in body  # Eastern time
    assert "最终以官网实际结账结果为准" in body
    assert FakeSMTP.instances[0].tls


def test_possibly_email_says_not_verified():
    notifier.send(POSSIBLY_AVAILABLE, S, NOW, [])
    (msg,) = sent_messages()
    assert msg["Subject"] == "🔔 VCA 手链疑似补货，请人工确认"
    assert "尚未验证可以成功在线购买" in msg.get_content()


def test_test_email_is_clearly_labelled():
    msg = notifier.build_message("TEST", S, NOW, [])
    assert "测试" in msg["Subject"] and "可以在线购买了" not in msg["Subject"]
    assert "不是补货通知" in msg.get_content()


def test_retries_transient_failure():
    FakeSMTP.fail_times = 2
    notifier.send(AVAILABLE, S, NOW, [])
    assert len(sent_messages()) == 1 and len(FakeSMTP.instances) == 3


def test_gives_up_after_limited_retries():
    FakeSMTP.fail_times = 99
    with pytest.raises(notifier.NotificationError):
        notifier.send(AVAILABLE, S, NOW, [], attempts=3)
    assert len(FakeSMTP.instances) == 3


def test_auth_failure_not_retried_and_no_secret_in_error():
    FakeSMTP.auth_fail = True
    with pytest.raises(notifier.NotificationError) as e:
        notifier.send(AVAILABLE, S, NOW, [])
    assert len(FakeSMTP.instances) == 1
    assert "app-password-123" not in str(e.value)


def test_not_configured():
    with pytest.raises(notifier.NotificationError):
        notifier.send(AVAILABLE, config.Settings(), NOW, [])


def test_password_not_in_repr():
    assert "app-password-123" not in repr(S)


def test_cli_test_mode(monkeypatch, capsys):
    for k, v in {"SMTP_HOST": "smtp.example.com", "SMTP_PORT": "465", "SMTP_USERNAME": "bot@example.com",
                 "SMTP_PASSWORD": "app-password-123", "NOTIFY_TO": "me@example.com"}.items():
        monkeypatch.setenv(k, v)
    assert notifier.main(["--test"]) == 0
    out = capsys.readouterr().out
    assert "app-password-123" not in out and "me@example.com" not in out
    assert "测试" in sent_messages()[0]["Subject"]
