"""Detect whether VCARD34700 can be bought online on the VCA US site.

How it works (verified on the live site, Nov 2026 — see README "Phase 1"):

The product page is loaded once in headless Chromium. While it loads, the page
itself requests ``<path>.productinfo.US.json`` which returns
``{"VCARD34700": {"sellable": true, "stock": false, ...}}``. The front-end uses
that to choose between the "ADD TO BAG" and "ORDER BY PHONE" call-to-action.
We observe that response and cross-check it against four independent page
signals:

* JSON-LD ``Product.offers.availability`` (InStock / OutOfStock)
* ``.vca-pdp-v2-controls[data-show-cta]`` ("add-to-bag" / "order-by-phone")
* whether an "ADD TO BAG" button is actually visible and enabled (it is always
  in the DOM, hidden with ``vca-hide`` when out of stock)
* dataLayer ``view_item`` ``item_available`` ("in stock" / "out of stock")

Only when all of them agree the item is in stock do we (optionally) click
"ADD TO BAG" in a throw-away anonymous session and check that the cart API
returns the right reference with quantity 1. Nothing is ever checked out.

Run directly for a one-off check:  ``python -m monitor.check_stock``
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from . import config

log = logging.getLogger("vca.check")

AVAILABLE = "AVAILABLE"
POSSIBLY_AVAILABLE = "POSSIBLY_AVAILABLE"
UNAVAILABLE = "UNAVAILABLE"
UNKNOWN = "UNKNOWN"
STATUSES = (AVAILABLE, POSSIBLY_AVAILABLE, UNAVAILABLE, UNKNOWN)

NEGATIVE_LD = {"outofstock", "soldout", "discontinued", "backorder", "preorder",
               "presale", "limitedavailability", "onlineonly_outofstock"}


@dataclass
class CartResult:
    attempted: bool = False
    ok: bool = False
    sku_found: bool = False
    quantity: int | None = None
    checkout_link_visible: bool = False
    error: str | None = None


@dataclass
class Signals:
    """Raw observations from one page load. All fields optional = 'not seen'."""
    http_status: int | None = None
    final_url: str | None = None
    error: str | None = None
    challenge: bool = False              # an access challenge is actually in force
    challenge_reason: str | None = None
    human_preloaded: bool = False        # HUMAN/PerimeterX script or hidden widget present (normal)
    title: str | None = None

    jsonld_sku: str | None = None
    jsonld_availability: str | None = None
    jsonld_currency: str | None = None

    productinfo_found: bool = False
    productinfo_sellable: bool | None = None
    productinfo_stock: bool | None = None

    page_country: str | None = None
    page_currency: str | None = None
    datalayer_item_available: str | None = None

    show_cta: str | None = None
    hide_cta: str | None = None
    ecom_enabled: str | None = None

    atb_present: bool = False
    atb_visible_enabled: bool = False
    atb_visible_disabled: bool = False
    oos_text: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    cart: CartResult | None = None


@dataclass
class CheckResult:
    status: str
    evidence: list[str]
    checked_at: str
    signals: dict[str, Any]
    strength: str | None = None       # POSSIBLY_AVAILABLE: "strong" | "weak"
    unknown_kind: str | None = None   # UNKNOWN: "blocked" | "error"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Classification (pure, unit-tested)
# ---------------------------------------------------------------------------

def _ld_value(av: str | None) -> bool | None:
    if not av:
        return None
    key = av.rsplit("/", 1)[-1].replace(" ", "").lower()
    if key == "instock":
        return True
    if key in NEGATIVE_LD:
        return False
    return None


def _datalayer_value(v: str | None) -> bool | None:
    if not v:
        return None
    v = v.strip().lower()
    if v == "in stock":
        return True
    if v in {"out of stock", "sold out", "unavailable"}:
        return False
    return None  # e.g. "TRUE" from static tracking attributes is not trusted


def _cta_value(sig: Signals) -> bool | None:
    if (sig.hide_cta or "").lower() == "true":
        return False
    cta = (sig.show_cta or "").lower()
    if cta == "add-to-bag":
        return True
    if cta in {"order-by-phone", "notify-me", "contact", "none", "appointment"}:
        return False
    return None


def _button_value(sig: Signals) -> bool | None:
    if sig.atb_visible_enabled:
        return True
    if sig.atb_present or sig.atb_visible_disabled:
        return False  # present in DOM but hidden or disabled
    return None


def _productinfo_value(sig: Signals) -> bool | None:
    if not sig.productinfo_found:
        return None
    if sig.productinfo_sellable is False or sig.productinfo_stock is False:
        return False
    if sig.productinfo_sellable and sig.productinfo_stock:
        return True
    return None


def stock_votes(sig: Signals) -> dict[str, bool | None]:
    return {
        "productinfo": _productinfo_value(sig),
        "jsonld": _ld_value(sig.jsonld_availability),
        "cta": _cta_value(sig),
        "button": _button_value(sig),
        "datalayer": _datalayer_value(sig.datalayer_item_available),
    }


def preconditions(sig: Signals, ref: str = config.PRODUCT_REF) -> tuple[bool, list[str]]:
    """Page loaded, not blocked, right product, right country."""
    if sig.error:
        return False, [f"页面加载失败: {sig.error}"]
    why: list[str] = []
    if sig.http_status is not None and sig.http_status != 200:
        why.append(f"HTTP 状态码 {sig.http_status}")
    if sig.final_url:
        path = urlparse(sig.final_url).path.lower()
        if not path.startswith("/us/en/") or ref.lower() not in path:
            why.append(f"页面被重定向到 {path}")
    if sig.jsonld_sku and sig.jsonld_sku.upper() != ref.upper():
        why.append(f"商品编号不匹配: 页面为 {sig.jsonld_sku}")
    if not sig.jsonld_sku and not sig.productinfo_found:
        why.append("页面上找不到商品编号")
    if sig.page_country and sig.page_country.upper() != config.COUNTRY:
        why.append(f"国家不是美国: {sig.page_country}")
    for cur in (sig.page_currency, sig.jsonld_currency):
        if cur and cur.upper() != config.CURRENCY:
            why.append(f"货币不是 USD: {cur}")
    return (not why, why)


def page_signals_positive(sig: Signals) -> bool:
    """Right page, and every mandatory stock signal positive with none negative."""
    ok, _ = preconditions(sig)
    if not ok:
        return False
    v = stock_votes(sig)
    if any(x is False for x in v.values()) or sig.oos_text:
        return False
    return v["productinfo"] is True and v["button"] is True and v["cta"] is True


def page_says_in_stock(sig: Signals) -> bool:
    """Ready for the add-to-bag check: page positive and no active challenge."""
    return not sig.challenge and page_signals_positive(sig)


def unknown_kind(sig: Signals) -> str:
    return "blocked" if sig.challenge or sig.http_status in (401, 403, 429) else "error"


def possibly_strength(sig: Signals) -> str:
    """'strong' = every page signal says in stock; only bag/challenge stopped AVAILABLE."""
    return "strong" if page_signals_positive(sig) else "weak"


def classify(sig: Signals, verify_cart: bool = True) -> tuple[str, list[str]]:
    if sig.challenge and sig.http_status not in (None, 200):
        return UNKNOWN, [f"访问被拦截: {sig.challenge_reason or 'challenge'} (HTTP {sig.http_status})"]
    ok, why = preconditions(sig)
    if not ok:
        if sig.challenge:
            why.insert(0, f"检测到访问挑战: {sig.challenge_reason or 'challenge'}")
        return UNKNOWN, why

    if sig.challenge:
        # A challenge is actually showing. Never AVAILABLE. If the stock data the
        # page did receive is unanimously positive, don't sit on it: escalate as
        # POSSIBLY so a human looks. Otherwise we cannot trust what we saw.
        if page_signals_positive(sig):
            return POSSIBLY_AVAILABLE, [f"{k}=有货" for k, v in stock_votes(sig).items() if v] + [
                f"访问挑战已触发（{sig.challenge_reason}），无法完成购物袋验证"]
        return UNKNOWN, [f"检测到访问挑战: {sig.challenge_reason or 'challenge'}，库存信号不可信"]

    votes = stock_votes(sig)
    pos = [k for k, v in votes.items() if v is True]
    neg = [k for k, v in votes.items() if v is False]
    if sig.oos_text:
        neg.append("oos_text")
    evidence = [f"{k}={'有货' if v else '无货'}" for k, v in votes.items() if v is not None]
    if sig.oos_text:
        evidence.append(f"页面文字: {', '.join(sig.oos_text)}")

    if not pos and not neg:
        return UNKNOWN, ["所有库存信号都缺失，网站结构可能已变化"]

    if not pos:
        # Need the data signal (productinfo or JSON-LD) plus one more to say "no".
        if ("productinfo" in neg or "jsonld" in neg) and len(neg) >= 2:
            return UNAVAILABLE, evidence
        return UNKNOWN, evidence + ["无货证据不足（只有单一信号）"]

    if neg or not page_says_in_stock(sig):
        return POSSIBLY_AVAILABLE, evidence + ["信号不一致或不完整，需人工确认"]

    # Every page signal says in stock.
    if not verify_cart:
        return AVAILABLE, evidence + ["购物袋验证已关闭"]
    cart = sig.cart
    if cart is None or not cart.attempted:
        return POSSIBLY_AVAILABLE, evidence + ["尚未完成购物袋验证"]
    if cart.ok and cart.sku_found and cart.quantity == 1:
        extra = "，结账入口可见" if cart.checkout_link_visible else ""
        return AVAILABLE, evidence + [f"已成功加入购物袋（编号匹配，数量 1{extra}）"]
    return POSSIBLY_AVAILABLE, evidence + [f"购物袋验证未通过: {cart.error or '未找到商品'}"]


# ---------------------------------------------------------------------------
# Cart response parsing (pure)
# ---------------------------------------------------------------------------

def _contains_ref(obj: Any, ref: str) -> bool:
    short = ref.upper().removeprefix("VC")
    if isinstance(obj, str):
        s = obj.upper()
        return s == ref.upper() or s == short or s.startswith(short)
    if isinstance(obj, dict):
        return any(_contains_ref(v, ref) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_ref(v, ref) for v in obj)
    return False


def parse_cart_response(status: int, data: Any, ref: str = config.PRODUCT_REF) -> CartResult:
    res = CartResult(attempted=True)
    if status >= 400:
        res.error = f"加入购物袋接口返回 HTTP {status}"
        return res
    if not isinstance(data, dict):
        res.error = "加入购物袋接口返回内容无法解析"
        return res
    if data.get("errors") or data.get("error"):
        res.error = f"加入购物袋接口报错: {str(data.get('errors') or data.get('error'))[:200]}"
        return res
    items = data.get("lineItems")
    if items is None and isinstance(data.get("cart"), dict):
        items = data["cart"].get("lineItems") or data["cart"].get("entries")
    if not isinstance(items, list):
        res.error = "购物袋响应中没有商品列表"
        return res
    for item in items:
        if _contains_ref(item, ref):
            res.sku_found = True
            try:
                res.quantity = int(item.get("quantity", 0))
            except (TypeError, ValueError):
                res.quantity = None
            break
    res.ok = res.sku_found and res.quantity == 1
    if not res.sku_found:
        res.error = "购物袋中没有找到正确的商品编号"
    elif res.quantity != 1:
        res.error = f"购物袋商品数量为 {res.quantity}"
    return res


# ---------------------------------------------------------------------------
# Browser part
# ---------------------------------------------------------------------------

EXTRACT_JS = r"""
() => {
  const vis = e => !!e && !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)
                 && !e.closest('.vca-hide') && getComputedStyle(e).visibility !== 'hidden';
  const lds = [...document.querySelectorAll('script[type="application/ld+json"]')]
    .map(s => { try { return JSON.parse(s.textContent) } catch (e) { return null } })
    .flatMap(o => Array.isArray(o) ? o : (o && o['@graph'] ? o['@graph'] : [o]))
    .filter(o => o && o['@type'] === 'Product');
  const p = lds[0] || null;
  const offers = p ? (Array.isArray(p.offers) ? p.offers[0] : p.offers) : null;
  const dl = Array.isArray(window.dataLayer) ? window.dataLayer : [];
  const ctx = dl.find(x => x && x.pg_country);
  const vi = [...dl].reverse().find(x => x && x.event === 'view_item');
  const viItem = vi && vi.ecommerce && vi.ecommerce.items && vi.ecommerce.items[0];
  const ctl = document.querySelector('.vca-pdp-v2-controls');
  const atbs = [...document.querySelectorAll('.vca-pdp-v2-add-to-bag-cta, button.vca-fea-add-cart')];
  const shown = atbs.filter(vis);
  const enabled = shown.filter(b => !b.disabled && b.getAttribute('aria-disabled') !== 'true');
  const text = document.body ? document.body.innerText : '';
  const lines = text.split('\n').map(s => s.trim().toLowerCase()).filter(Boolean);
  const oos = [...new Set(lines.filter(l => /^(out of stock|sold out|notify me|currently unavailable|no longer available)$/.test(l)))];
  // An access challenge counts only if it is really in front of the user:
  // rendered, non-trivial size, inside the viewport, not transparent/aria-hidden,
  // and the top-most element at its centre. A hidden, preloaded HUMAN widget
  // (present on normal VCA pages) does not count.
  const inFront = e => {
    if (!vis(e)) return false;
    const r = e.getBoundingClientRect();
    if (r.width < 40 || r.height < 16) return false;
    if (r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth) return false;
    for (let a = e; a && a.nodeType === 1; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (parseFloat(cs.opacity) < 0.05 || cs.visibility === 'hidden' || a.getAttribute('aria-hidden') === 'true') return false;
    }
    const x = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1);
    const y = Math.min(Math.max(r.top + r.height / 2, 0), innerHeight - 1);
    const hit = document.elementFromPoint(x, y);
    return !!hit && (hit === e || e.contains(hit) || hit.contains(e));
  };
  const CAPTCHA_SEL = '#px-captcha, [id^="px-captcha"], [class*="px-captcha"], #challenge-form, ' +
    'iframe[src*="captcha"], iframe[src*="px-cdn"], iframe[src*="px-cloud"], iframe[src*="perimeterx"]';
  const captchaEls = [...document.querySelectorAll(CAPTCHA_SEL)];
  const activeCaptcha = captchaEls.find(inFront);
  const holdEls = [...document.querySelectorAll('body *')].filter(e => e.children.length === 0 &&
    /^press\s*(&|and)\s*hold$/i.test((e.textContent || '').trim()));
  const activeHold = holdEls.find(inFront);
  const title = document.title || '';
  const blockedTitle = /access denied|access to this page has been denied|pardon our interruption|request unsuccessful|are you a robot/i.test(title);
  const preloaded = !!(window._pxAppId || captchaEls.length || holdEls.length ||
    document.querySelector('script[src*="px-cdn"], script[src*="px-cloud"], script[src*="perimeterx"], script[src*="/px/client"]'));
  return {
    title,
    jsonld_sku: p ? (p.sku || null) : null,
    jsonld_availability: offers ? (offers.availability || null) : null,
    jsonld_currency: offers ? (offers.priceCurrency || null) : null,
    page_country: ctx ? ctx.pg_country : null,
    page_currency: ctx ? (ctx.currency || null) : null,
    datalayer_item_available: viItem ? (viItem.item_available || null) : null,
    show_cta: ctl ? (ctl.dataset.showCta ?? null) : null,
    hide_cta: ctl ? (ctl.dataset.hideCta ?? null) : null,
    ecom_enabled: ctl ? (ctl.dataset.ecomEnabled ?? null) : null,
    atb_present: atbs.length > 0,
    atb_visible_enabled: enabled.length > 0,
    atb_visible_disabled: shown.length > 0 && enabled.length === 0,
    oos_text: oos,
    captcha: !!(blockedTitle || activeCaptcha || activeHold),
    captcha_reason: blockedTitle ? 'block page: ' + title : (activeCaptcha ? 'visible captcha element'
      : (activeHold ? 'visible "Press & Hold" challenge' : null)),
    human_preloaded: preloaded,
  };
}
"""


def _apply_productinfo(sig: Signals, payload: Any, ref: str) -> None:
    if isinstance(payload, dict):
        entry = payload.get(ref) or payload.get(ref.upper())
        if isinstance(entry, dict):
            sig.productinfo_found = True
            s, st = entry.get("sellable"), entry.get("stock")
            sig.productinfo_sellable = s if isinstance(s, bool) else None
            sig.productinfo_stock = st if isinstance(st, bool) else None


def _load_once(browser, settings: config.Settings, url: str, ref: str) -> Signals:
    sig = Signals()
    ctx = browser.new_context(locale="en-US", timezone_id="America/New_York",
                              viewport={"width": 1366, "height": 900})
    try:
        page = ctx.new_page()
        page.set_default_timeout(settings.page_timeout_ms)
        product_path = urlparse(url).path.removesuffix(".html")
        info_responses: list = []

        def on_response(resp):
            p = urlparse(resp.url).path
            if p == product_path + config.PRODUCTINFO_SUFFIX:
                info_responses.append(resp)

        page.on("response", on_response)
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=settings.page_timeout_ms)
        except Exception as e:  # network error, timeout
            sig.error = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
            return sig
        sig.http_status = resp.status if resp else None
        sig.final_url = page.url

        # Wait for the page's own stock request, then let the UI settle.
        deadline = time.monotonic() + settings.settle_ms / 1000 + 10
        while not info_responses and time.monotonic() < deadline:
            page.wait_for_timeout(500)
        page.wait_for_timeout(settings.settle_ms)

        api_blocked = None
        for r in info_responses:
            if r.status in (401, 403, 429):
                api_blocked = f"库存接口被拦截 (HTTP {r.status})"
                continue
            try:
                if r.status == 200:
                    _apply_productinfo(sig, r.json(), ref)
            except Exception as e:
                # An HTML page instead of JSON from this endpoint is a challenge page.
                ctype = (r.headers or {}).get("content-type", "")
                if "html" in ctype:
                    api_blocked = "库存接口返回了网页而不是数据（疑似访问挑战）"
                else:
                    sig.warnings.append(f"productinfo 无法解析: {type(e).__name__}")

        data = page.evaluate(EXTRACT_JS)
        for k in ("title", "jsonld_sku", "jsonld_availability", "jsonld_currency",
                  "page_country", "page_currency", "datalayer_item_available",
                  "show_cta", "hide_cta", "ecom_enabled", "atb_present",
                  "atb_visible_enabled", "atb_visible_disabled", "oos_text"):
            setattr(sig, k, data.get(k))
        sig.oos_text = list(sig.oos_text or [])
        sig.human_preloaded = bool(data.get("human_preloaded"))
        if data.get("captcha"):
            sig.challenge, sig.challenge_reason = True, data.get("captcha_reason")
        elif api_blocked:
            sig.challenge, sig.challenge_reason = True, api_blocked
        elif sig.human_preloaded:
            sig.warnings.append("HUMAN/PerimeterX 组件已预加载但未触发（正常）")
        if sig.http_status in (401, 403, 429):
            sig.challenge = True
            sig.challenge_reason = sig.challenge_reason or f"HTTP {sig.http_status}"

        if settings.verify_cart and page_says_in_stock(sig):
            sig.cart = _verify_cart(page, ref)
        return sig
    finally:
        ctx.close()


def _verify_cart(page, ref: str) -> CartResult:
    """Click ADD TO BAG once in this throw-away session and read the cart API reply."""
    try:
        btn = page.locator(".vca-pdp-v2-add-to-bag-cta:not(.vca-hide):visible").first
        with page.expect_response(
            lambda r: config.ADD_TO_CART_PATH in r.url and r.request.method == "POST",
            timeout=20_000,
        ) as info:
            btn.click(timeout=10_000)
        r = info.value
        try:
            data = r.json()
        except Exception:
            data = None
        result = parse_cart_response(r.status, data, ref)
        page.wait_for_timeout(2_000)
        try:
            result.checkout_link_visible = page.get_by_text("PROCEED TO CHECKOUT", exact=False).first.is_visible()
        except Exception:
            pass
        # Best effort: empty the anonymous bag again. Never go to checkout.
        try:
            page.locator(".vca-fea-cart-item-remove:visible").first.click(timeout=5_000)
        except Exception:
            pass
        return result
    except Exception as e:
        return CartResult(attempted=True, error=f"{type(e).__name__}: {str(e).splitlines()[0][:200]}")


def run_check(settings: config.Settings | None = None, url: str = config.PRODUCT_URL,
              ref: str = config.PRODUCT_REF, browser_factory=None) -> CheckResult:
    """Load the page (with limited retries) and classify. Never raises."""
    settings = settings or config.load_settings()
    from playwright.sync_api import sync_playwright

    sig = Signals(error="not run")
    with sync_playwright() as pw:
        try:
            if browser_factory:
                browser = browser_factory(pw)
            else:
                kw: dict[str, Any] = {"headless": settings.headless}
                if settings.browser_channel:
                    kw["channel"] = settings.browser_channel
                browser = pw.chromium.launch(**kw)
        except Exception as e:
            sig = Signals(error=f"浏览器启动失败: {type(e).__name__}: {str(e)[:200]}")
        else:
            try:
                for attempt in range(1, settings.max_attempts + 1):
                    sig = _load_once(browser, settings, url, ref)
                    transient = sig.error is not None or (sig.http_status or 0) >= 500
                    log.info("attempt %d: http=%s error=%s challenge=%s", attempt,
                             sig.http_status, sig.error, sig.challenge)
                    if not transient or attempt == settings.max_attempts:
                        break
                    time.sleep(20 * attempt)  # gentle backoff, never hammer the site
            finally:
                browser.close()

    status, evidence = classify(sig, settings.verify_cart)
    return CheckResult(
        status=status,
        evidence=evidence,
        checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        signals=asdict(sig),
        strength=possibly_strength(sig) if status == POSSIBLY_AVAILABLE else None,
        unknown_kind=unknown_kind(sig) if status == UNKNOWN else None,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = run_check()
    print(result.to_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
