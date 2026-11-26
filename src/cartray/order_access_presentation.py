"""Bounded branding for the native accountless order page; never access authority."""
import base64
import hashlib
import json
import re
from html import escape

from cartray.merchant_contract import merchant_path

# Existing GEL/system stacks only: no CSS values, font URLs or remote font loading.
ORDER_HEADING_FONTS = {
    'modern': 'Arial, Helvetica, sans-serif',
    'editorial': 'Georgia, "Times New Roman", serif',
    'humanist': '-apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif',
    'system': 'system-ui, sans-serif',
}


def validate_order_presentation(value):
    if (not isinstance(value, dict)
            or value.keys() - {"name", "home_path", "contact_path", "accent", "logo_path", "heading_font"}):
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
    logo = result.get('logo_path')
    if 'logo_path' in result and (not isinstance(logo, str) or len(logo) > 512
            or not re.fullmatch(r'/[A-Za-z0-9_./-]+\.(?:svg|png|jpe?g|webp)', logo, re.IGNORECASE | re.ASCII)
            or any(part in ('', '.', '..') for part in logo.split('/')[1:])):
        raise ValueError('order access logo must be an approved same-origin static image path')
    if 'heading_font' in result and (not isinstance(result['heading_font'], str)
                                      or result['heading_font'] not in ORDER_HEADING_FONTS):
        raise ValueError('invalid order access heading font')
    # Optional fields stay absent when omitted: existing four-field binding bytes remain stable.
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
.cr-order-brand{font-size:1.25rem;font-weight:bold;overflow-wrap:anywhere}
.cr-order-home{display:inline-flex;align-items:center;min-height:48px}
.cr-order-logo{display:block;width:224px;height:56px;max-width:100%;object-fit:contain;object-position:left center}
.cr-order h1,.cr-order h2{font-family:__HEADING_FONT__;line-height:1.2}
.cr-order h1{margin:0 0 1rem;font-size:clamp(1.8rem,4vw,2.5rem)}
.cr-order{background:#fff;border-radius:1rem;overflow-wrap:anywhere}
a{color:__INK__}.cr-order-item{margin-block:1rem;padding-block:1rem;border-top:1px solid #ccd3d8}
.cr-order-item-header{display:flex;align-items:baseline;justify-content:space-between;gap:.75rem;flex-wrap:wrap}
.cr-order-item-title{margin:0;font-size:1.25rem;flex:1 1 18rem}
.cr-order-quantity{margin:0;white-space:nowrap;font-size:.9rem;color:#424b50}
.cr-order-resources{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,18rem),1fr));
list-style:none;gap:.5rem;margin:.75rem 0 0;padding:0}
.cr-resource-form{margin:0}.cr-button{display:inline-flex;align-items:center;justify-content:center;
min-height:48px;max-width:100%;padding:.65rem 1.25rem;border:2px solid __INK__;border-radius:2rem;
font:inherit;font-weight:700;line-height:1.4;text-align:center;overflow-wrap:anywhere;cursor:pointer;text-decoration:none}
.cr-button--primary{background:__ACCENT__;color:__TEXT__}
.cr-button--resource{width:100%;justify-content:space-between;gap:1rem;text-align:left;border-radius:.5rem}
.cr-resource-name{overflow-wrap:anywhere;min-width:0}.cr-button--secondary{background:white;color:__INK__}
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
    for (const [itemIndex, item] of data.items.entries()) {
      const section = document.createElement('article'), heading = document.createElement('h2');
      const header = document.createElement('header'); header.className='cr-order-item-header';
      section.className='cr-order-item'; heading.className='cr-order-item-title';
      heading.id=`cr-order-item-${itemIndex}`;heading.textContent=item.title;
      section.setAttribute('aria-labelledby',heading.id);
      const quantity = document.createElement('p'); quantity.className='cr-order-quantity';
      quantity.textContent = `Quantity: ${item.quantity}`;header.append(heading,quantity);section.append(header);
      const resources = document.createElement('ul');resources.className='cr-order-resources';
      resources.setAttribute('role','list');resources.setAttribute('aria-label','Purchased resources');
      for (const resource of item.resources) {
        const form = document.createElement('form'); form.className='cr-resource-form';
        form.method = 'post'; form.action = location.pathname;
        for (const [name, value] of [['token', token], ['resource', resource.index]]) {
          const input = document.createElement('input'); input.type = 'hidden'; input.name = name; input.value = value;
          form.append(input);
        }
        const entry = document.createElement('li');entry.className='cr-order-resource';
        const button = document.createElement('button');
        const name = document.createElement('span');name.className='cr-resource-name';name.textContent=resource.name;
        const arrow = document.createElement('span');arrow.textContent='→';arrow.setAttribute('aria-hidden','true');
        button.append(name,arrow);button.dataset.crAction='open-resource';
        button.className='cr-button cr-button--primary cr-button--resource'; button.disabled = !resource.available;
        button.dataset.crState=resource.available?'available':'unavailable';
        form.append(button);entry.append(form);resources.append(entry);
      }
      if(item.resources.length)section.append(resources);
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
    label = escape(config['name'])
    if config.get('logo_path'):
        label = ('<img class="cr-order-logo" width="224" height="56" referrerpolicy="no-referrer" src="'
                 + escape(config['logo_path'], quote=True) + '" alt="' + escape(config['name'], quote=True) + '">')
    brand = '<a class="cr-order-home" href="' + escape(config['home_path'], quote=True) + '">' + label + '</a>'
    contact = ('<p class="cr-order-contact"><a class="cr-button cr-button--secondary" href="'
               + escape(config['contact_path'], quote=True) + '">Contact the store</a></p>'
               if config['contact_path'] else '')
    css = ORDER_ACCESS_CSS.replace('__ACCENT__', config['accent']).replace('__TEXT__', _contrast_text(config['accent']))
    css = css.replace('__HEADING_FONT__', ORDER_HEADING_FONTS[config.get('heading_font', 'modern')])
    css = css.replace('__INK__', config['accent'] if _contrast_text(config['accent']) == '#ffffff' else '#20272d')
    replacements = {'BRAND': brand, 'CONTACT': contact}
    page = re.sub(r'__(BRAND|CONTACT)__', lambda match: replacements[match[1]], ORDER_ACCESS_PAGE)
    page = page.replace('<script>', '<style>' + css + '</style><script>', 1)
    script = page.split('<script>', 1)[1].split('</script>', 1)[0]
    policy = ("default-src 'none'; script-src " + _hash(script) + "; style-src " + _hash(css)
              + "; connect-src 'self'; base-uri 'none'" + ("; img-src 'self'" if config.get('logo_path') else ''))
    return page, policy
