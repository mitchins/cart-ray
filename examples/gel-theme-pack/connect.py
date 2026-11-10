#!/usr/bin/env python3
"""Apply this presentation to an existing Acorn build without changing server code.

No provisioning, API calls, activation or secret access. Writes a NEW site.
Native review and management clients are copied byte-for-byte from the repo.
CartRay's app is copied byte-for-byte with its declarative presenter guard;
checkout, persistence, settlement-return algorithms and exports are unmodified.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
if __package__:
    from .build import ROOT, build_site, write
else:
    from build import ROOT, build_site, write

BOOT='if (typeof document !== "undefined" && !document.querySelector(\'[data-cartray-presenter="gel"]\')) boot();'
EXPORTS=('loadCatalogue','loadPersistedCart','persistCart','addToCart','createCheckoutAndStorePending','processCheckoutReturn','pollCheckoutStatus','confirmedCheckoutView','expiredCheckoutView','unconfirmedCheckoutView','cancelledCheckoutView')


def connect(source_site,cartray_root,output,gel,catalogue,theme,assets_root=ROOT):
    source_site,cartray_root,output=map(Path,(source_site,cartray_root,output))
    if output.exists():raise ValueError('output already exists; choose a new directory')
    source_site=source_site.resolve();cartray_root=cartray_root.resolve()
    if output.resolve().is_relative_to(source_site) or source_site.is_relative_to(output.resolve()):
        raise ValueError('source and output directories must not overlap')
    if not (source_site/'storefront-config.js').is_file():raise ValueError('source site must be an existing built Acorn storefront')
    config_text=(source_site/'storefront-config.js').read_text()
    match=re.search(r'window\.CARTRAY_STOREFRONT\s*=\s*',config_text)
    if not match:raise ValueError('unsupported storefront config format; integrate the presenter explicitly')
    source_config,_=json.JSONDecoder().raw_decode(config_text[match.end():])
    content=json.loads(Path(catalogue).read_text())
    for p in content['products']:
        key=p['product_key']; expected='/products/'+p['slug']+'/'
        if source_config.get('productPaths',{}).get(key)!=expected:raise ValueError(f'product route mismatch for {key}')
        if source_config.get('reviewSubjects',{}).get(key)!=p['review_subject']:raise ValueError(f'review subject mismatch for {key}')
    if set(source_config.get('productPaths',{}))!={p['product_key'] for p in content['products']}:raise ValueError('theme content must match every product route in the existing build')
    files={
        'theme/core-storefront.js':source_site/'app.js',
        'theme/native-reviews.js':cartray_root/'modules/reviews/ui/reviews.js',
        'theme/native-manage.js':cartray_root/'modules/reviews/ui/manage.js',
    }
    for path in files.values():
        if not path.is_file():raise ValueError(f'missing required source file: {path}')
    core=files['theme/core-storefront.js'].read_text()
    if core.count(BOOT)!=1:raise ValueError('CartRay presenter guard changed; review the integration before applying')
    for name in EXPORTS:
        if not re.search(r'export\s+(?:async\s+)?function\s+'+name+r'\(',core):raise ValueError(f'CartRay presenter ABI changed: {name}')
    if not (source_site/'reviews.mjs').is_file():raise ValueError('existing public Reviews reader is required')
    build_site(output,gel,catalogue,theme,assets_root=assets_root,preview=False)
    # Retain route gateway, configuration and unrelated static content from the known build.
    for file in source_site.rglob('*'):
        if file.is_symlink():raise ValueError('source build must not contain symlinks')
        relative=file.relative_to(source_site);dest=output/relative
        if file.is_file() and not dest.exists():dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(file,dest)
    preserved={}
    for destination,path in files.items():
        raw=path.read_bytes();preserved[str(path.relative_to(source_site) if path.is_relative_to(source_site) else path.relative_to(cartray_root))]=hashlib.sha256(raw).hexdigest()
        if destination.endswith('core-storefront.js'):
            shutil.copyfile(path,output/destination)
        else:shutil.copyfile(path,output/destination)
    shutil.copyfile(source_site/'storefront-config.js',output/'storefront-config.js')
    shutil.copyfile(source_site/'reviews.mjs',output/'reviews.mjs')
    # Keep the upstream gateway and header policy. Add only privacy headers, never weaken it.
    existing_headers=(source_site/'_headers').read_text() if (source_site/'_headers').is_file() else ''
    write(output/'_headers',existing_headers+'\n'+(output/'_headers').read_text())
    for file in output.rglob('*.html'):
        html=file.read_text()
        if 'site-config.js' not in html:continue
        html=html.replace('<body ', '<body data-cartray-presenter="gel" ')
        html=html.replace('<script src="/site-config.js" defer>','<script src="/storefront-config.js" defer></script><script src="/site-config.js" defer>')
        html=re.sub(r'<span class="price">.*?</span>','<span class="price">Price loading…</span>',html,flags=re.S)
        html=re.sub(r'<p class="detail-price">.*?</p>','<p class="detail-price">Price loading…</p>',html,flags=re.S)
        html=re.sub(r'(<button[^>]*\bdata-add="[^"]+")',r'\1 disabled',html)
        write(file,html)
    # The origin's existing POST unsubscribe route remains authoritative; do not replace it.
    write(output/'integration-receipt.json',json.dumps({'source_files_sha256':preserved,'core_edit':'none; declarative GEL presenter disables core auto-boot','preview_adapter_present':False,'mode':'connected, existing storefront flags unchanged','qualification':'Run the original repository suites and hosted sandbox tests before deployment.'},indent=2)+'\n')
    # Refresh complete receipt after copying upstream code/config.
    paths={str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.rglob('*')) if p.is_file() and p.name!='build-receipt.json'}
    write(output/'build-receipt.json',json.dumps({'theme':theme,'preview':False,'files':paths},indent=2,sort_keys=True)+'\n')
    print(f'Connected presentation written to {output}; no deployment or feature activation performed.')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-site',type=Path,required=True)
    p.add_argument('--cartray-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--gel',type=Path,default=ROOT/'gel/squirrel.json')
    p.add_argument('--catalogue',type=Path,default=ROOT/'content/squirrel.json')
    p.add_argument('--theme',choices=('minimal','studio','classic'),default='minimal')
    a=p.parse_args()
    connect(a.source_site,a.cartray_root,a.output,a.gel,a.catalogue,a.theme)
if __name__=='__main__':main()
