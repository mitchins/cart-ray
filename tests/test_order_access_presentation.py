import base64
import hashlib
from types import SimpleNamespace

import pytest

from cartray.order_access_presentation import render_order_access_page, validate_order_presentation


def test_branded_order_page_is_escaped_and_exactly_hash_authorized():
    import json
    config = {'name': '<Store> & __CONTACT__', 'home_path': '/catalogue/',
              'contact_path': '/contact-us/', 'accent': '#001b35'}
    page, csp = render_order_access_page(SimpleNamespace(CARTRAY_ORDER_ACCESS_PRESENTATION=json.dumps(config)))
    assert '&lt;Store&gt; &amp; __CONTACT__' in page
    assert 'href="/catalogue/"' in page and 'href="/contact-us/"' in page
    assert "'unsafe-inline'" not in csp and 'style-src' in csp
    for tag in ('script', 'style'):
        body = page.split('<' + tag + '>', 1)[1].split('</' + tag + '>', 1)[0]
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        assert "'sha256-" + digest + "'" in csp
    assert "form-action" not in csp  # Preserve purchased external-link POST redirects.
    assert 'cr-resource-form' in page and 'resource.index' in page
    assert 'sessionStorage' not in page and 'localStorage' not in page


@pytest.mark.parametrize('value', [{'name': ''}, {'home_path': '//evil.test/'},
    {'contact_path': '/contact/?token=private'}, {'accent': 'red;display:none'}, {'stylesheet': '/any.css'}])
def test_unsafe_branding_is_rejected(value):
    with pytest.raises(ValueError):
        validate_order_presentation(value)


def test_light_brand_colour_keeps_text_and_link_contrast():
    page, _ = render_order_access_page(SimpleNamespace(CARTRAY_ORDER_ACCESS_PRESENTATION='{"accent":"#ffff00"}'))
    assert 'background:#ffff00;color:#000000' in page
    assert 'a{color:#20272d}' in page


def test_operator_binding_encoding_and_renderer_limit_agree_for_long_unicode_branding():
    from cartray.order_access_presentation import order_presentation_binding
    path = '/' + 'a' * 250 + '/' + 'b' * 250 + '/'
    config = {'name': '🌿' * 100, 'home_path': path, 'contact_path': path}
    encoded = order_presentation_binding(config)
    assert len(encoded.encode()) <= 2048
    page, _ = render_order_access_page(SimpleNamespace(CARTRAY_ORDER_ACCESS_PRESENTATION=encoded))
    assert '🌿' * 100 in page
