"""End-to-end detection through real headless Chromium, against local fixtures.

Every request to vancleefarpels.com is intercepted (page.route) – no internet.
These prove the extraction + classification code path works on pages shaped like
the real one. They do NOT prove the live site is reachable from GitHub Actions.
"""

import os
import shutil

import pytest

from monitor import config
from monitor.check_stock import AVAILABLE, POSSIBLY_AVAILABLE, UNAVAILABLE, UNKNOWN, run_check
from tests import fixtures as fx

pytestmark = pytest.mark.browser

CHROME = os.environ.get("TEST_CHROME") or shutil.which("google-chrome") or shutil.which("chromium")
INFO_URL = config.PRODUCT_URL.removesuffix(".html") + ".productinfo.US.json"

SETTINGS = config.Settings(settle_ms=400, page_timeout_ms=15_000, max_attempts=1, verify_cart=True)


def _factory(routes):
    def factory(pw):
        kw = {"headless": True}
        if CHROME:
            kw["executable_path"] = CHROME
        browser = pw.chromium.launch(**kw)
        orig_new_context = browser.new_context

        def new_context(**kwargs):
            ctx = orig_new_context(**kwargs)

            def handler(route):
                url = route.request.url
                for match, resp in routes.items():
                    if match in url:
                        if resp == "ABORT":
                            return route.abort("connectionrefused")
                        status, body = resp
                        ctype = "application/json" if body.lstrip().startswith(("{", "[")) else "text/html"
                        return route.fulfill(status=status, body=body, content_type=ctype)
                return route.fulfill(status=404, body="not found")

            ctx.route("**/*", handler)
            return ctx

        browser.new_context = new_context
        return browser
    return factory


def check(routes, settings=SETTINGS):
    return run_check(settings, browser_factory=_factory(routes))


PAGE_URL_KEY = "bracelet-5-motifs.html"
INFO_KEY = "bracelet-5-motifs.productinfo.US.json"
CART_KEY = config.ADD_TO_CART_PATH


def test_1_available_with_cart_verification():
    r = check({PAGE_URL_KEY: (200, fx.product_page(availability="InStock")),
               INFO_KEY: (200, fx.productinfo(stock=True)),
               CART_KEY: (200, fx.cart_ok())})
    assert r.status == AVAILABLE, r.evidence
    assert r.signals["cart"]["sku_found"] and r.signals["cart"]["quantity"] == 1
    assert r.signals["cart"]["checkout_link_visible"]


def test_2_unavailable_matches_live_site_pattern():
    r = check({PAGE_URL_KEY: (200, fx.product_page(availability="OutOfStock")),
               INFO_KEY: (200, fx.productinfo(stock=False))})
    assert r.status == UNAVAILABLE, r.evidence
    s = r.signals
    assert s["atb_present"] and not s["atb_visible_enabled"]  # hidden button in DOM
    assert s["show_cta"] == "order-by-phone"
    assert s["cart"] is None  # never touches the bag when out of stock


def test_3_possibly_available_when_signals_disagree():
    # stock API says yes, JSON-LD (server cache) still says OutOfStock
    r = check({PAGE_URL_KEY: (200, fx.product_page(availability="OutOfStock")),
               INFO_KEY: (200, fx.productinfo(stock=True))})
    assert r.status == POSSIBLY_AVAILABLE, r.evidence


def test_4_page_load_failure_is_unknown():
    r = check({PAGE_URL_KEY: "ABORT"})
    assert r.status == UNKNOWN
    assert r.signals["error"]


def test_4b_http_500_is_unknown():
    r = check({PAGE_URL_KEY: (500, "<html><body>error</body></html>")})
    assert r.status == UNKNOWN


def test_5_captcha_is_unknown():
    r = check({PAGE_URL_KEY: (200, fx.CAPTCHA_PAGE)})
    assert r.status == UNKNOWN
    assert r.signals["challenge"]


def test_5b_http_403_block_is_unknown():
    r = check({PAGE_URL_KEY: (403, fx.CAPTCHA_PAGE)})
    assert r.status == UNKNOWN


def test_6_sku_mismatch_is_unknown():
    r = check({PAGE_URL_KEY: (200, fx.product_page(sku="VCARA41300", availability="InStock")),
               INFO_KEY: (200, fx.productinfo(stock=True))})
    assert r.status == UNKNOWN
    assert any("不匹配" in e for e in r.evidence)


def test_6b_wrong_country_is_unknown():
    r = check({PAGE_URL_KEY: (200, fx.product_page(country="FR", availability="InStock")),
               INFO_KEY: (200, fx.productinfo(stock=True))})
    assert r.status == UNKNOWN


def test_7_button_visible_but_disabled_is_not_available():
    r = check({PAGE_URL_KEY: (200, fx.product_page(availability="InStock", disabled=True)),
               INFO_KEY: (200, fx.productinfo(stock=True))})
    assert r.signals["atb_visible_disabled"]
    assert r.status == POSSIBLY_AVAILABLE, r.evidence


def test_7b_button_shown_while_out_of_stock_is_not_available():
    r = check({PAGE_URL_KEY: (200, fx.product_page(availability="OutOfStock", force_show=True)),
               INFO_KEY: (200, fx.productinfo(stock=False))})
    assert r.status in (POSSIBLY_AVAILABLE, UNAVAILABLE)
    assert r.status != AVAILABLE


def test_8_add_to_bag_failure_downgrades_to_possibly():
    r = check({PAGE_URL_KEY: (200, fx.product_page(availability="InStock")),
               INFO_KEY: (200, fx.productinfo(stock=True)),
               CART_KEY: (409, '{"errors":[{"code":"OUT_OF_STOCK"}]}')})
    assert r.status == POSSIBLY_AVAILABLE
    assert r.signals["cart"]["attempted"] and not r.signals["cart"]["ok"]


def test_8b_cart_returns_wrong_item():
    r = check({PAGE_URL_KEY: (200, fx.product_page(availability="InStock")),
               INFO_KEY: (200, fx.productinfo(stock=True)),
               CART_KEY: (200, fx.cart_ok(ref="VCARA41300"))})
    assert r.status == POSSIBLY_AVAILABLE


def test_stock_api_missing_and_ld_missing_is_unknown():
    r = check({PAGE_URL_KEY: (200, fx.product_page(include_ld=False))})
    assert r.status == UNKNOWN


# ---- HUMAN / PerimeterX: preloaded vs. actually challenging ------------------

def test_preloaded_human_widget_does_not_block_unavailable():
    r = check({PAGE_URL_KEY: (200, fx.product_page(extra=fx.PRELOADED_HUMAN)),
               INFO_KEY: (200, fx.productinfo(stock=False))})
    assert r.signals["human_preloaded"] is True
    assert r.signals["challenge"] is False
    assert r.status == UNAVAILABLE, r.evidence


def test_preloaded_human_widget_does_not_block_available():
    r = check({PAGE_URL_KEY: (200, fx.product_page(availability="InStock", extra=fx.PRELOADED_HUMAN)),
               INFO_KEY: (200, fx.productinfo(stock=True)),
               CART_KEY: (200, fx.cart_ok())})
    assert r.signals["challenge"] is False
    assert r.status == AVAILABLE, r.evidence


def test_active_challenge_over_out_of_stock_page_is_unknown_blocked():
    r = check({PAGE_URL_KEY: (200, fx.product_page(extra=fx.ACTIVE_HUMAN)),
               INFO_KEY: (200, fx.productinfo(stock=False))})
    assert r.signals["challenge"] is True
    assert r.status == UNKNOWN and r.unknown_kind == "blocked"


def test_active_challenge_over_in_stock_page_is_possibly_strong_never_available():
    r = check({PAGE_URL_KEY: (200, fx.product_page(availability="InStock", extra=fx.ACTIVE_HUMAN)),
               INFO_KEY: (200, fx.productinfo(stock=True)),
               CART_KEY: (200, fx.cart_ok())})
    assert r.status == POSSIBLY_AVAILABLE and r.strength == "strong"
    assert r.signals["cart"] is None          # we never click through a challenge


def test_stock_endpoint_blocked_403_is_unknown():
    r = check({PAGE_URL_KEY: (200, fx.product_page()),
               INFO_KEY: (403, '{"blocked": true}')})
    assert r.signals["challenge"] is True and r.status == UNKNOWN and r.unknown_kind == "blocked"


def test_stock_endpoint_returns_challenge_html_is_unknown():
    r = check({PAGE_URL_KEY: (200, fx.product_page()),
               INFO_KEY: (200, fx.CAPTCHA_PAGE)})
    assert r.signals["challenge"] is True and r.status == UNKNOWN


def test_cart_failure_gives_strong_possibly():
    r = check({PAGE_URL_KEY: (200, fx.product_page(availability="InStock")),
               INFO_KEY: (200, fx.productinfo(stock=True)),
               CART_KEY: (500, '{"errors": ["x"]}')})
    assert r.status == POSSIBLY_AVAILABLE and r.strength == "strong"


def test_signal_disagreement_gives_weak_possibly():
    r = check({PAGE_URL_KEY: (200, fx.product_page(availability="OutOfStock")),
               INFO_KEY: (200, fx.productinfo(stock=True))})
    assert r.status == POSSIBLY_AVAILABLE and r.strength == "weak"


def test_blocked_page_is_unknown_blocked():
    r = check({PAGE_URL_KEY: (403, fx.CAPTCHA_PAGE)})
    assert r.unknown_kind == "blocked"
