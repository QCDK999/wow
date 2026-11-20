from monitor.check_stock import (AVAILABLE, POSSIBLY_AVAILABLE, UNAVAILABLE, UNKNOWN, CartResult,
                                 Signals, classify, parse_cart_response)

URL = "https://www.vancleefarpels.com/us/en/collections/jewelry/alhambra/vcard34700---vintage-alhambra-bracelet-5-motifs.html"


def live_oos() -> Signals:
    """Exactly what the live US page showed on 2026-11-19."""
    return Signals(http_status=200, final_url=URL, jsonld_sku="VCARD34700",
                   jsonld_availability="OutOfStock", jsonld_currency="USD",
                   productinfo_found=True, productinfo_sellable=True, productinfo_stock=False,
                   page_country="US", page_currency="USD", datalayer_item_available="out of stock",
                   show_cta="order-by-phone", hide_cta="false", ecom_enabled="true",
                   atb_present=True, atb_visible_enabled=False)


def in_stock(cart=True) -> Signals:
    s = live_oos()
    s.jsonld_availability, s.productinfo_stock = "InStock", True
    s.datalayer_item_available, s.show_cta, s.atb_visible_enabled = "in stock", "add-to-bag", True
    if cart:
        s.cart = CartResult(attempted=True, ok=True, sku_found=True, quantity=1, checkout_link_visible=True)
    return s


def test_available():
    assert classify(in_stock())[0] == AVAILABLE


def test_available_requires_cart_when_enabled():
    assert classify(in_stock(cart=False))[0] == POSSIBLY_AVAILABLE
    assert classify(in_stock(cart=False), verify_cart=False)[0] == AVAILABLE


def test_live_snapshot_is_unavailable():
    status, ev = classify(live_oos())
    assert status == UNAVAILABLE
    assert any("productinfo" in e for e in ev)


def test_single_negative_signal_is_not_enough():
    s = Signals(http_status=200, final_url=URL, jsonld_sku="VCARD34700", jsonld_availability="OutOfStock")
    assert classify(s)[0] == UNKNOWN


def test_order_by_phone_alone_is_not_out_of_stock_evidence():
    # On in-stock pages "ORDER BY PHONE" is also visible – we never look at it.
    s = in_stock()
    assert classify(s)[0] == AVAILABLE


def test_static_tracking_true_is_ignored():
    s = live_oos()
    s.datalayer_item_available = "TRUE"  # misleading value present in raw HTML
    assert classify(s)[0] == UNAVAILABLE


def test_sellable_false_is_negative():
    s = in_stock()
    s.productinfo_sellable = False
    assert classify(s)[0] == POSSIBLY_AVAILABLE


def test_notify_me_text_blocks_available():
    s = in_stock()
    s.oos_text = ["notify me"]
    assert classify(s)[0] == POSSIBLY_AVAILABLE


def test_unknown_never_from_errors_to_available():
    for mutate in (lambda s: setattr(s, "error", "Timeout"),
                   lambda s: setattr(s, "http_status", 503),
                   lambda s: setattr(s, "jsonld_sku", "VCARA41300"),
                   lambda s: setattr(s, "page_country", "FR"),
                   lambda s: setattr(s, "final_url", "https://www.vancleefarpels.com/us/en/home.html")):
        s = in_stock()
        mutate(s)
        assert classify(s)[0] == UNKNOWN


def test_structure_change_is_unknown():
    s = Signals(http_status=200, final_url=URL, jsonld_sku="VCARD34700")
    assert classify(s)[0] == UNKNOWN


def test_parse_cart():
    ok = parse_cart_response(200, {"lineItems": [{"quantity": 1, "sku": "VCARD34700"}]})
    assert ok.ok and ok.sku_found and ok.quantity == 1
    local = parse_cart_response(200, {"lineItems": [{"quantity": 1, "attributes": [
        {"name": "localReference", "value": "ARD34700"}]}]})
    assert local.ok
    assert not parse_cart_response(200, {"lineItems": []}).ok
    assert not parse_cart_response(200, {"lineItems": [{"quantity": 2, "sku": "VCARD34700"}]}).ok
    assert not parse_cart_response(500, None).ok
    assert not parse_cart_response(200, {"errors": ["OUT_OF_STOCK"]}).ok
    assert not parse_cart_response(200, "garbage").ok


def test_active_challenge_never_available():
    # Active challenge over unanimously positive data: escalate, never AVAILABLE.
    s = in_stock()
    s.challenge, s.challenge_reason = True, 'visible "Press & Hold" challenge'
    status, ev = classify(s)
    assert status == POSSIBLY_AVAILABLE and any("访问挑战" in e for e in ev)
    # Active challenge over negative / mixed data: cannot be trusted → UNKNOWN.
    s = live_oos()
    s.challenge = True
    assert classify(s)[0] == UNKNOWN
    # Blocked document (403) → UNKNOWN regardless of anything else.
    s = in_stock()
    s.challenge, s.http_status = True, 403
    assert classify(s)[0] == UNKNOWN


def test_preloaded_human_widget_is_not_a_challenge():
    s = live_oos()
    s.human_preloaded = True
    assert classify(s)[0] == UNAVAILABLE
    s = in_stock()
    s.human_preloaded = True
    assert classify(s)[0] == AVAILABLE
