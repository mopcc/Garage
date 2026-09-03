import asyncio, hashlib, json, os, re
from pathlib import Path
from urllib.parse import urlparse, unquote
from playwright.async_api import async_playwright

TARGET=os.getenv('TARGET_URL','https://www.murphybeds.com/pages/buildabed')
OUT=Path(os.getenv('OUTPUT_DIR','murphybed_assets'))
ASSET=OUT/'assets'; ASSET.mkdir(parents=True,exist_ok=True)
network=[]; static_urls=set(); manifest=[]; hashes={}

def static_candidate(url,ct=''):
    if not url.startswith('http'): return False
    host=urlparse(url).netloc.lower(); path=urlparse(url).path.lower()
    if 'recommended-build-' in url.lower(): return False
    if 'murphy-beds-models.b-cdn.net' in host: return True
    if ('murphy-beds-retail.myshopify.com' in host or 'cdn.shopify' in host) and path.endswith('.svg'): return True
    return False

def clean_name(url, fallback):
    name=Path(unquote(urlparse(url).path)).name
    name=re.sub(r'[^A-Za-z0-9._-]+','_',name)
    return name if name and len(name)<180 else fallback

async def download_one(ctx,url):
    try:
        r=await ctx.request.get(url,timeout=10000)
        if not r.ok: return {'url':url,'error':f'HTTP {r.status}'}
        b=await asyncio.wait_for(r.body(),timeout=10)
        if not b: return {'url':url,'error':'empty'}
        h=hashlib.sha256(b).hexdigest(); ext=Path(urlparse(url).path).suffix.lower() or '.bin'
        if h in hashes: fn=hashes[h]
        else:
            fn=f'{h[:10]}__{clean_name(url,f"{h[:20]}{ext}")}'
            (ASSET/fn).write_bytes(b); hashes[h]=fn
        return {'url':url,'file':f'assets/{fn}','sha256':h,'bytes':len(b),'content_type':r.headers.get('content-type',''),'status':r.status}
    except Exception as ex: return {'url':url,'error':repr(ex)}

async def click_text(page,text):
    total=0
    for fr in page.frames:
        try:
            n=await fr.evaluate("""needle=>{const norm=s=>(s||'').trim().replace(/\\s+/g,' ').toLowerCase();let n=0;for(const el of document.querySelectorAll('button,[role=button],[role=radio],label,span')){if(norm(el.textContent)!==norm(needle))continue;const t=el.closest('button,[role=button],[role=radio],label')||el;if((t.href||'').match(/pages\\/buildabed/i))continue;try{t.click();n++}catch(e){}}return n;}""",text)
            total+=n or 0
        except: pass
    if total: print('CLICK',text,total,flush=True); await page.wait_for_timeout(700)
    return total

async def dump(page):
    try: (OUT/'page_text.txt').write_text(await page.locator('body').inner_text(timeout=5000),encoding='utf-8')
    except: pass
    rows=[]
    for fi,fr in enumerate(page.frames):
        try:
            data=await fr.evaluate("""()=>[...document.querySelectorAll('button,a,input,label,select,[role=button],[role=radio],[role=option],[tabindex]')].map((el,i)=>({i,tag:el.tagName.toLowerCase(),type:el.type||'',text:(el.innerText||el.textContent||'').trim().replace(/\\s+/g,' ').slice(0,220),aria:el.getAttribute('aria-label')||'',title:el.title||'',name:el.name||'',value:el.value||'',id:el.id||'',cls:(el.className||'').toString().slice(0,220),href:el.href||'',role:el.getAttribute('role')||''}))""")
            for r in data: r.update(frame=fi,frame_url=fr.url); rows.append(r)
        except: pass
    (OUT/'controls.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')

async def main():
    diag={'target':TARGET}
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        ctx=await browser.new_context(viewport={'width':1800,'height':1400},user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152 Safari/537.36')
        page=await ctx.new_page()
        def on_resp(r):
            try:
                u=r.url; ct=r.headers.get('content-type','')
                network.append({'url':u,'status':r.status,'content_type':ct})
                if static_candidate(u,ct): static_urls.add(u)
            except: pass
        page.on('response',on_resp)
        await page.goto(TARGET,wait_until='domcontentloaded',timeout=60000)
        await page.wait_for_timeout(8000)
        diag.update(title=await page.title(),url=page.url)
        await dump(page)
        for text in ['Vertical','Horizontal','Twin','Full','Queen','None','Left','Right','Both','Open','Closed']:
            await click_text(page,text)
        await page.wait_for_timeout(3500)
        await dump(page)
        urls=sorted(static_urls); print('CONFIGURATOR CDN URLS',len(urls),flush=True)
        sem=asyncio.Semaphore(10)
        async def bounded(u):
            async with sem:return await download_one(ctx,u)
        manifest.extend(await asyncio.gather(*(bounded(u) for u in urls)))
        ok=[r for r in manifest if r.get('file')]
        diag.update(static_cdn_urls=len(urls),downloaded_assets=len(ok),unique_files=len(hashes),bunny_cdn_assets=sum('b-cdn.net' in r['url'] for r in ok),glb_assets=sum(r['file'].endswith('.glb') for r in ok),shopify_svg_assets=sum(r['file'].endswith('.svg') for r in ok))
        (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        (OUT/'network.json').write_text(json.dumps(network,indent=2),encoding='utf-8')
        (OUT/'urls.txt').write_text('\n'.join(urls)+'\n',encoding='utf-8')
        (OUT/'diagnostics.json').write_text(json.dumps(diag,indent=2),encoding='utf-8')
        print(json.dumps(diag,indent=2),flush=True)
        await browser.close()

if __name__=='__main__': asyncio.run(main())
