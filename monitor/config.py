"""Configuration. Everything sensitive comes from environment variables (GitHub Secrets)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# --- Monitored product (US site only) ---------------------------------------
PRODUCT_REF = "VCARD34700"
PRODUCT_NAME = "Vintage Alhambra Bracelet, 5 Motifs"
PRODUCT_MATERIAL = "18K White Gold / Chalcedony"
PRODUCT_URL = (
    "https://www.vancleefarpels.com/us/en/collections/jewelry/alhambra/"
    "vcard34700---vintage-alhambra-bracelet-5-motifs.html"
)
COUNTRY = "US"
CURRENCY = "USD"

# The product page itself requests "<page path>.productinfo.US.json" to decide
# whether to show "Add to bag". We only *observe* that response; we never call
# it separately.
PRODUCTINFO_SUFFIX = ".productinfo.US.json"
ADD_TO_CART_PATH = "/api/commerce/unified/addCartLineItem"


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Detection
    check_interval_minutes: int = 60
    page_timeout_ms: int = 45_000
    settle_ms: int = 6_000          # time for client-side stock call to finish
    max_attempts: int = 2           # page-load attempts per run
    verify_cart: bool = True        # add-to-bag check, only when page says in stock
    headless: bool = True
    browser_channel: str | None = None
    monitor_paused: bool = False

    # Email (all from secrets)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_from: str = ""
    notify_to: str = ""
    smtp_security: str = "starttls"  # starttls | ssl | none
    notify_possibly: bool = True
    possibly_max_emails: int = 6

    # Health: alert after N consecutive UNKNOWN results, remind every N hours.
    health_alert_after: int = 3
    health_remind_hours: int = 24
    # Optional dead-man's switch (e.g. a free healthchecks.io check URL). Pinged
    # every healthy run; "/fail" when degraded. If runs stop entirely (GitHub
    # disabled the schedule, Actions outage) that service emails you.
    healthcheck_url: str = field(default="", repr=False)

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_port and self.smtp_username
                    and self.smtp_password and self.notify_to)

    @property
    def policy(self):
        from .state import Policy
        return Policy(alert_after=self.health_alert_after, health_remind_hours=self.health_remind_hours,
                      possibly_max_emails=self.possibly_max_emails, allow_possibly=self.notify_possibly)

    @property
    def sender(self) -> str:
        return self.smtp_from or self.smtp_username


def load_settings() -> Settings:
    return Settings(
        check_interval_minutes=max(60, _int("CHECK_INTERVAL_MINUTES", 60)),
        page_timeout_ms=_int("PAGE_TIMEOUT_MS", 45_000),
        settle_ms=_int("SETTLE_MS", 6_000),
        max_attempts=min(3, max(1, _int("MAX_ATTEMPTS", 2))),
        verify_cart=_bool("VERIFY_CART", True),
        headless=_bool("HEADLESS", True),
        browser_channel=os.environ.get("BROWSER_CHANNEL") or None,
        monitor_paused=_bool("MONITOR_PAUSED", False),
        smtp_host=os.environ.get("SMTP_HOST", "").strip(),
        smtp_port=_int("SMTP_PORT", 587),
        smtp_username=os.environ.get("SMTP_USERNAME", "").strip(),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        smtp_from=os.environ.get("SMTP_FROM", "").strip(),
        notify_to=os.environ.get("NOTIFY_TO", "").strip(),
        smtp_security=(os.environ.get("SMTP_SECURITY", "") or "").strip().lower()
        or ("ssl" if _int("SMTP_PORT", 587) == 465 else "starttls"),
        notify_possibly=_bool("NOTIFY_POSSIBLY", True),
        possibly_max_emails=min(20, max(1, _int("POSSIBLY_MAX_EMAILS", 6))),
        health_alert_after=min(48, max(1, _int("HEALTH_ALERT_AFTER", 3))),
        health_remind_hours=min(168, max(1, _int("HEALTH_REMIND_HOURS", 24))),
        healthcheck_url=os.environ.get("HEALTHCHECK_URL", "").strip(),
    )
