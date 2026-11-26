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
    assert 'href="/catalogue/"' in page
    assert 'href="/contact-us/"' in page
    assert "'unsafe-inline'" not in csp
    assert 'style-src' in csp
    for tag in ('script', 'style'):
        body = page.split('<' + tag + '>', 1)[1].split('</' + tag + '>', 1)[0]
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        assert "'sha256-" + digest + "'" in csp
    assert "form-action" not in csp  # Preserve purchased external-link POST redirects.
    assert 'cr-resource-form' in page
    assert 'resource.index' in page
    assert 'sessionStorage' not in page
    assert 'localStorage' not in page


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


@pytest.mark.parametrize('logo', ['https://outside.test/logo.svg', '//outside.test/logo.svg',
    '/assets/../logo.svg', '/assets//logo.svg', '/logo.svg?token=private', '/logo.svg#private',
    '/assets/logo.html', '/assets/%2e%2e/logo.svg', '/assets/logo.svg\n',
    '/brand/logo.ſvg', '/assets/loKo.svg', 1, True, []])
def test_order_logo_rejects_non_static_or_non_local_values(logo):
    with pytest.raises(ValueError, match='logo'):
        validate_order_presentation({'logo_path': logo})


@pytest.mark.parametrize('font', ['Cardo', 'url(https://outside.test/font)', '</style><script>', None, [], {}])
def test_heading_font_is_a_fixed_stack_choice(font):
    with pytest.raises(ValueError, match='font'):
        validate_order_presentation({'heading_font': font})


def test_existing_branding_binding_does_not_acquire_optional_fields():
    import json

    from cartray.order_access_presentation import order_presentation_binding
    old = {'name': 'Merchant', 'home_path': '/catalogue/', 'contact_path': '/help/', 'accent': '#001b35'}
    assert order_presentation_binding(old) == json.dumps(old, ensure_ascii=False, separators=(',', ':'))
    assert 'logo_path' not in validate_order_presentation(old)
    assert 'heading_font' not in validate_order_presentation(old)


@pytest.mark.parametrize('font', ['modern', 'editorial', 'humanist', 'system'])
def test_optional_logo_and_curated_font_have_exact_csp_without_remote_fonts(font):
    import json

    from cartray.order_access_presentation import ORDER_HEADING_FONTS
    config = {'name': '<Merchant> " &', 'logo_path': '/_astro/approved-logo.123.svg', 'heading_font': font}
    page, csp = render_order_access_page(SimpleNamespace(CARTRAY_ORDER_ACCESS_PRESENTATION=json.dumps(config)))
    assert 'src="/_astro/approved-logo.123.svg"' in page
    assert 'alt="&lt;Merchant&gt; &quot; &amp;"' in page
    assert ORDER_HEADING_FONTS[font] in page
    assert "img-src 'self'" in csp
    assert 'font-src' not in csp
    assert "unsafe-inline" not in csp
    assert 'form-action' not in csp
    for tag in ('script', 'style'):
        body = page.split('<' + tag + '>', 1)[1].split('</' + tag + '>', 1)[0]
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        assert "'sha256-" + digest + "'" in csp
    assert 'cr-order-resources' in page
    assert 'cr-resource-name' in page
