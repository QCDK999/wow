"""Fake VCA product pages that mirror the structure observed on the live site."""

import json
from string import Template

PAGE = Template(r"""<!DOCTYPE html>
<html lang="en-US"><head><title>$title</title>
<script type="application/ld+json">$ld</script>
<style>.vca-hide{display:none}</style></head>
<body>
$captcha
<section class="vca-pdp-v2-controls" data-ecom-enabled="true" data-hide-cta="false">
  <div class="vca-pdp-v2-inline-ctas">
    <button class="vca-fea-add-cart vca-pdp-v2-cta-common vca-pdp-v2-add-to-bag-cta vca-hide" $disabled
            data-product-in-stock="Product is not available anymore">ADD TO BAG</button>
    <a class="vca-pdp-v2-order-by-phone-cta-mobile-label" href="tel:+18778262533">ORDER BY PHONE</a>
  </div>
</section>
$extra
<div id="minicart" style="display:none"><a href="#">PROCEED TO CHECKOUT</a>
  <button class="vca-fea-cart-item-remove">remove</button></div>
<script>
window.dataLayer = [{pg_country: "$country", currency: "USD"}];
fetch(location.pathname.replace('.html', '.productinfo.US.json'))
  .then(r => r.json()).then(j => {
    const e = j["$ref"]; const inStock = !!(e && e.sellable && e.stock);
    const ctl = document.querySelector('.vca-pdp-v2-controls');
    ctl.dataset.showCta = $force_cta || (inStock ? 'add-to-bag' : 'order-by-phone');
    if (inStock || $force_show) document.querySelectorAll('.vca-pdp-v2-add-to-bag-cta')
        .forEach(b => b.classList.remove('vca-hide'));
    dataLayer.push({event: 'view_item', ecommerce: {items: [{item_available: inStock ? 'in stock' : 'out of stock'}]}});
  }).catch(() => {});
document.querySelector('.vca-pdp-v2-add-to-bag-cta').addEventListener('click', async () => {
  await fetch('/api/commerce/unified/addCartLineItem', {method: 'POST', body: '{}'});
  document.getElementById('minicart').style.display = 'block';
});
</script></body></html>""")


def product_page(*, ref="VCARD34700", sku=None, availability="OutOfStock", country="US",
                 title="Vintage Alhambra bracelet, 5 motifs - Van Cleef & Arpels",
                 disabled=False, force_show=False, force_cta=None, captcha=False,
                 extra="", include_ld=True) -> str:
    ld = {"@context": "https://schema.org/", "@type": "Product", "name": "Vintage Alhambra bracelet, 5 motifs",
          "sku": sku or ref, "offers": {"@type": "Offer", "availability": availability,
                                        "price": "6200.0", "priceCurrency": "USD"}}
    return PAGE.substitute(
        title=title, ld=json.dumps(ld) if include_ld else "{}", country=country, ref=ref,
        disabled="disabled" if disabled else "", force_show="true" if force_show else "false",
        force_cta=json.dumps(force_cta) if force_cta else "null",
        captcha='<div id="px-captcha">Press &amp; Hold</div>' if captcha else "", extra=extra)


def productinfo(ref="VCARD34700", sellable=True, stock=False) -> str:
    return json.dumps({ref: {"articleType": "single", "sellable": sellable, "stock": stock,
                             "lowStock": False, "price": "6200.00"}})


def cart_ok(ref="VCARD34700", qty=1) -> str:
    return json.dumps({"id": "cart-1", "lineItems": [
        {"quantity": qty, "attributes": [{"name": "localReference", "value": ref.removeprefix("VC")}],
         "sku": ref}]})


CAPTCHA_PAGE = """<!DOCTYPE html><html><head><title>Access to this page has been denied</title></head>
<body><div id="px-captcha">Press &amp; Hold</div><p>Press & Hold</p></body></html>"""


# HUMAN/PerimeterX present but NOT challenging: script tag, hidden container,
# off-screen and transparent "Press & Hold" widgets, and a widget covered by
# another element. Normal VCA pages carry this kind of preloaded markup.
PRELOADED_HUMAN = """
<script>window._pxAppId = 'PXtest';</script>
<script src="https://client.px-cloud.net/PXtest/main.min.js" async></script>
<div id="px-captcha" style="display:none"><p>Press &amp; Hold</p></div>
<button style="position:absolute;left:-9999px;top:0;width:200px;height:50px">Press &amp; Hold</button>
<div style="position:fixed;right:0;bottom:0;width:300px;height:80px;opacity:0"><span>Press &amp; Hold</span></div>
<div style="position:fixed;right:0;bottom:100px;width:320px;height:90px">
  <div id="px-captcha-modal" style="position:absolute;inset:0;z-index:1">x</div>
  <div style="position:absolute;inset:0;background:#fff;z-index:2"></div>
</div>
"""

# An access challenge actually in front of the user.
ACTIVE_HUMAN = """
<div style="position:fixed;inset:0;background:rgba(255,255,255,.96);z-index:9999;
            display:flex;align-items:center;justify-content:center">
  <div id="px-captcha" style="width:310px;height:100px"><p>Press &amp; Hold</p></div>
</div>
"""
