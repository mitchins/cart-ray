"""Bounded branding for the native accountless order page; never access authority."""
import base64
import hashlib
import json
import re
from html import escape

from cartray.merchant_contract import merchant_path


def validate_order_presentation(value):
    if not isinstance(value, dict) or value.keys() - {"name", "home_path", "contact_path", "accent"}:
        raise ValueError("invalid order access presentation fields")
    result = {"name": "Your store", "home_path": "/", "contact_path": None, "accent": "#185a75", **value}
    if (not isinstance(result['name'], str) or not result['name'].strip() or len(result['name']) > 100
            or any(ord(c) < 32 for c in result['name'])):
        raise ValueError("invalid order access merchant name")
    for field in ('home_path', 'contact_path'):
        path = result[field]
        if field == 'contact_path' and path is None:
            continue
        if path != '/':
            merchant_path(path)
    if not isinstance(result['accent'], str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', result['accent']):
        raise ValueError("order access accent must be a six-digit hex colour")
    if len(_encode(result).encode()) > 2048:
        raise ValueError('order access presentation exceeds the 2048-byte binding limit')
    return result


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def order_presentation_binding(value):
    return _encode(validate_order_presentation(value))


def _contrast_text(accent):
    values = [int(accent[i:i+2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in values]
    luminance = sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722), strict=True))
    return '#000000' if luminance > .179 else '#ffffff'


def _hash(value):
    return "'sha256-" + base64.b64encode(hashlib.sha256(value.encode()).digest()).decode() + "'"


ORDER_ACCESS_CSS = """
:root{font:100%/1.6 Arial,sans-serif;color:#20272d;background:#f5f6f7}
*{box-sizing:border-box}body{margin:0}.cr-order-brand,.cr-order{max-width:52rem;margin:auto;padding:1.5rem}
.cr-order-brand{font-size:1.25rem;font-weight:bold;overflow-wrap:anywhere}.cr-order{background:#fff;border-radius:1rem;overflow-wrap:anywhere}
a{color:__INK__}.cr-order-item{margin-block:1.5rem;padding-block:1rem;border-top:1px solid #ccd3d8}
.cr-resource-form{margin-block:.75rem}.cr-button{display:inline-flex;align-items:center;justify-content:center;
min-height:48px;max-width:100%;padding:.65rem 1.25rem;border:2px solid __INK__;border-radius:2rem;
font:inherit;font-weight:700;line-height:1.4;text-align:center;overflow-wrap:anywhere;cursor:pointer;text-decoration:none}
.cr-button--primary{background:__ACCENT__;color:__TEXT__}.cr-button--secondary{background:white;color:__INK__}
.cr-button:disabled{background:#e9edef;color:#424b50;border-color:#74818a;cursor:not-allowed}
:focus-visible{outline:3px solid #111;outline-offset:4px}.cr-order-contact{margin-block:1.5rem}
@media(max-width:360px){.cr-order,.cr-order-brand{padding:1rem}}
"""

ORDER_ACCESS_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Your order</title>
<header class="cr-order-brand">__BRAND__</header><main class="cr-order"><h1>Your order</h1>
<section id="content" aria-live="polite" aria-busy="true" data-cr-state="loading">
Loading your order…</section>__CONTACT__</main>
<noscript>JavaScript is required to view your order.</noscript>
<script>
(() => {
  const content = document.getElementById('content');
  const token = new URLSearchParams(location.hash.slice(1)).get('token');
  let busy=false;
  async function load() {
  if(busy)return;
  if (!token || !/^[A-Za-z0-9_-]{43}$/.test(token)) {
    content.textContent = 'This order link is unavailable.';
    content.dataset.crState='invalid';content.setAttribute('aria-busy','false'); return; }
  busy=true;content.textContent='Loading your order…';content.dataset.crState='loading';
  content.setAttribute('aria-busy','true');
  try {
    const response = await fetch(location.pathname, {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({token}), credentials:'omit', cache:'no-store',signal:AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error();
    const data = await response.json(); content.replaceChildren();
    content.dataset.crState='ready';content.setAttribute('aria-busy','false');
    if (data.reviews) {
      const link = document.createElement('a'); link.href = data.reviews.url;
      link.className='cr-button cr-button--secondary';
      link.textContent = 'Review your purchased products'; content.append(link);
    }
    if(!data.items.length){const empty=document.createElement('p');
      empty.textContent='No purchased resources are currently listed.';content.append(empty);}
    for (const item of data.items) {
      const section = document.createElement('section'), heading = document.createElement('h2');
      section.className='cr-order-item'; heading.textContent = item.title; section.append(heading);
      const quantity = document.createElement('p'); quantity.textContent = `Quantity: ${item.quantity}`;
      section.append(quantity);
      for (const resource of item.resources) {
        const form = document.createElement('form'); form.className='cr-resource-form';
        form.method = 'post'; form.action = location.pathname;
        for (const [name, value] of [['token', token], ['resource', resource.index]]) {
          const input = document.createElement('input'); input.type = 'hidden'; input.name = name; input.value = value;
          form.append(input);
        }
        const button = document.createElement('button'); button.textContent = resource.name;
        button.className='cr-button cr-button--primary'; button.disabled = !resource.available;
        button.dataset.crState=resource.available?'available':'unavailable';
        form.append(button); section.append(form);
      }
      content.append(section);
    }
  } catch { content.textContent = 'Your order is temporarily unavailable. Please try again later.';
    content.dataset.crState='unavailable'; content.setAttribute('aria-busy','false');
    const retry=document.createElement('button');retry.textContent='Try again';
    retry.className='cr-button cr-button--secondary';retry.type='button';
    retry.addEventListener('click',()=>{void load();});content.append(retry);
  } finally {busy=false;}
  }
  void load();
})();
</script></html>"""


def render_order_access_page(env):
    raw = getattr(env, 'CARTRAY_ORDER_ACCESS_PRESENTATION', None)
    if raw is None:
        raw = '{}'
    if not isinstance(raw, str) or len(raw.encode()) > 2048:
        raise ValueError('invalid order access presentation')
    config = validate_order_presentation(json.loads(raw))
    brand = '<a href="' + escape(config['home_path'], quote=True) + '">' + escape(config['name']) + '</a>'
    contact = ('<p class="cr-order-contact"><a class="cr-button cr-button--secondary" href="'
               + escape(config['contact_path'], quote=True) + '">Contact the store</a></p>'
               if config['contact_path'] else '')
    css = ORDER_ACCESS_CSS.replace('__ACCENT__', config['accent']).replace('__TEXT__', _contrast_text(config['accent']))
    css = css.replace('__INK__', config['accent'] if _contrast_text(config['accent']) == '#ffffff' else '#20272d')
    replacements = {'BRAND': brand, 'CONTACT': contact}
    page = re.sub(r'__(BRAND|CONTACT)__', lambda match: replacements[match[1]], ORDER_ACCESS_PAGE)
    page = page.replace('<script>', '<style>' + css + '</style><script>', 1)
    script = page.split('<script>', 1)[1].split('</script>', 1)[0]
    policy = ("default-src 'none'; script-src " + _hash(script) + "; style-src " + _hash(css)
              + "; connect-src 'self'; base-uri 'none'")
    return page, policy
