"""Offline browser loader for restricted CI environments.

Loads the actual generated HTML/CSS/assets/application source into Chromium with
no network. Only ESM loading and location.assign are replaced with test seams.
It is not a substitute for HTTP deployment, Safari or a screen-reader audit.
"""
from pathlib import Path
import base64
import json
import re
from urllib.parse import urlsplit,unquote

class OfflineSite:
    def __init__(self,page,dist):
        self.page=page; self.dist=Path(dist);self.storage={'local':{},'session':{}}
        self.errors=[];page.on('pageerror',lambda e:self.errors.append(str(e)))
        self.assets={}
        for p in self.dist.rglob('assets/*'):
            if p.is_file():
                mime='image/svg+xml' if p.suffix=='.svg' else 'image/png'
                self.assets['/'+str(p.relative_to(self.dist))]='data:'+mime+';base64,'+base64.b64encode(p.read_bytes()).decode()
    def open(self,path):
        try:self.storage=self.page.evaluate('window.__snapshot ? window.__snapshot() : ({local:{},session:{}})')
        except Exception:pass
        self.page.goto('about:blank')
        parts=urlsplit(path); relative=unquote(parts.path).lstrip('/')
        file=self.dist/relative
        if file.is_dir():file=file/'index.html'
        html=file.read_text()
        site_name=relative.split('/')[0]
        site=self.dist/site_name
        for href in re.findall(r'<link rel="stylesheet" href="([^"]+)">',html):
            css=(self.dist/href.lstrip('/')).read_text()
            for a,data in self.assets.items():css=css.replace(a,data)
            html=html.replace(f'<link rel="stylesheet" href="{href}">','<style>'+css+'</style>')
        html=re.sub(r'<script\b[^>]*>.*?</script>','',html,flags=re.S)
        html=re.sub(r'<link rel="icon"[^>]*>','',html)
        for asset,data in self.assets.items():html=html.replace('src="'+asset+'"','src="'+data+'"')
        self.page.set_content(html,wait_until='domcontentloaded')
        self.page.evaluate("suffix=>history.replaceState(null,'','about:blank'+suffix)",('?'+parts.query if parts.query else '')+('#'+parts.fragment if parts.fragment else ''))
        self.page.evaluate('''data=>{
          function store(values){const o={...values};Object.defineProperties(o,{
            getItem:{value:k=>Object.hasOwn(o,k)?o[k]:null},setItem:{value:(k,v)=>{o[k]=String(v)}},
            removeItem:{value:k=>{delete o[k]}},clear:{value:()=>{Object.keys(o).forEach(k=>delete o[k])}},
            key:{value:n=>Object.keys(o)[n]||null},length:{get:()=>Object.keys(o).length}
          });return o;}
          const local=store(data.local),session=store(data.session);
          Object.defineProperty(window,'localStorage',{value:local,configurable:true});
          Object.defineProperty(window,'sessionStorage',{value:session,configurable:true});
          window.__snapshot=()=>({local:{...local},session:{...session}});
          window.__navigate=url=>{window.__lastNavigation=url};
          document.addEventListener('click',e=>{const a=e.target.closest('a');if(a&&!a.getAttribute('href').startsWith('#')&&!a.href.startsWith('mailto:')){e.preventDefault();window.__lastNavigation=a.getAttribute('href')}},true);
        }''',self.storage)
        self.page.evaluate('''assets=>{
          const fix=()=>document.querySelectorAll('img[src]').forEach(e=>{const data=assets[e.getAttribute('src')];if(data)e.setAttribute('src',data)});
          new MutationObserver(fix).observe(document,{subtree:true,childList:true,attributes:true,attributeFilter:['src']});fix();
        }''',self.assets)
        config=(site/'site-config.js').read_text()
        self.page.add_script_tag(content=config)
        demo=(site/'theme/demo-adapter.mjs').read_text().replace('export function ','function ').replace('export async function ','async function ')
        self.page.add_script_tag(content='(()=>{'+demo+';window.__adapter={createAdapter};})()')
        app=(site/'theme/app.mjs').read_text().replace('const module=await import(config.adapter);','const module=window.__adapter;').replace('location.assign(','window.__navigate(')
        self.page.add_script_tag(content='(()=>{'+app+'})()')
        self.page.wait_for_timeout(80)
        self.page.evaluate('async()=>{await document.fonts.ready;await Promise.all([...document.images].map(i=>i.decode().catch(()=>{})))}')
        return self.page
    def follow(self):
        path=self.page.evaluate('window.__lastNavigation')
        assert path,'No navigation requested'
        return self.open(path)
    def reset(self):
        self.storage={'local':{},'session':{}}
        self.page.evaluate('()=>{if(window.localStorage)window.localStorage.clear();if(window.sessionStorage)window.sessionStorage.clear()}')
