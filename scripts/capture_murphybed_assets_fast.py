import asyncio, hashlib, json, os, re
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

TARGET=os.getenv('TARGET_URL','https://www.murphybeds.com/pages/buildabed')
OUT=Path(os.getenv('OUTPUT_DIR','murphybed_assets'))
ASSET=OUT/'assets'; ASSET.mkdir(parents=True,exist_ok=True)
seen_hash={}; manifest=[]; network=[]; tasks=set()
EXTS={'.png','.jpg','.jpeg','.webp','.avif','.gif','.svg','.glb','.gltf','.hdr','.bin'}
CT_EXT={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp','image/avif':'.avif','image/gif':'.gif','image/svg+xml':'.svg','model/gltf-binary':'.glb','model/gltf+json':'.gltf'}

def want(url,ct):
    ext=Path(urlparse(url).path).suffix.lower(); ct=(ct or '').lower()
    return ct.startswith('image/') or ct.startswith('model/') or ext in EXTS or 'murphy-beds-models.b-cdn.net' in url

def ext_for(url,ct):
    e=Path(urlparse(url).path).suffix.lower()
    return e if e in EXTS else CT_EXT.get((ct or '').split(';')[0].lower(),'.bin')

async def save_response(resp):
    try:
        ct=(resp.headers.get('content-type') or '').lower(); u=resp.url
        network.append({'url':u,'status':resp.status,'content_type':ct})
        if not want(u,ct): return
        b=await resp.body()
        if not b: return
        h=hashlib.sha256(b).hexdigest(); e=ext_for(u,ct)
        if h not in seen_hash:
            fn=f'{h[:20]}{e}'; (ASSET/fn).write_bytes(b); seen_hash[h]=fn
        else: fn=seen_hash[h]
        manifest.append({'url':u,'file':f'assets/{fn}','sha256':h,'bytes':len(b),'content_type':ct,'status':resp.status})
    except Exception as ex:
        network.append({'url':getattr(resp,'url','?'),'error':str(ex)})

async def click_text(page,text):
    total=0
    for fr in page.frames:
        try:
            n=await fr.evaluate("""
            needle=>{const norm=s=>(s||'').trim().replace(/\\s+/g,' ').toLowerCase();let n=0;
              for(const el of document.querySelectorAll('button,[role=button],[role=radio],label,span,div')){
                if(norm(el.textContent)!==norm(needle)) continue;
                const t=el.closest('button,[role=button],[role=radio],label')||el;
                if((t.href||'').match(/pages\\/buildabed/i)) continue;
                try{t.click();n++}catch(e){}
              } return n;}
            """,text)
            total+=n or 0
        except: pass
    if total:
        print('CLICK',text,total,flush=True); await page.wait_for_timeout(1300)
    return total

async def dump(page):
    try: (OUT/'page_text.txt').write_text(await page.locator('body').inner_text(timeout=5000),encoding='utf-8')
    except: pass
    rows=[]
    for fi,fr in enumerate(page.frames):
        try:
            data=await fr.evaluate("""
            ()=>[...document.querySelectorAll('button,a,input,label,select,[role=button],[role=radio],[role=option],[tabindex]')].map((el,i)=>({i,tag:el.tagName.toLowerCase(),type:el.type||'',text:(el.innerText||el.textContent||'').trim().replace(/\\s+/g,' ').slice(0,220),aria:el.getAttribute('aria-label')||'',title:el.title||'',name:el.name||'',value:el.value||'',id:el.id||'',cls:(el.className||'').toString().slice(0,220),href:el.href||'',role:el.getAttribute('role')||''}))
            """)
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
            t=asyncio.create_task(save_response(r));tasks.add(t);t.add_done_callback(tasks.discard)
        page.on('response',on_resp)
        await page.goto(TARGET,wait_until='domcontentloaded',timeout=60000)
        await page.wait_for_timeout(10000)
        diag.update(title=await page.title(),url=page.url)
        await dump(page)

        # Current configurator option labels. Stay on the lowercase page; do not click Build Your Own link.
        for text in ['Vertical','Horizontal','Twin','Full','Queen','None','Left','Right','Both','Open','Closed']:
            await click_text(page,text)
        await page.wait_for_timeout(5000)

        # Click visible configurator controls by keyword, but never navigation links.
        kw=re.compile(r'cabinet|finish|door|side|width|open|closed|vertical|horizontal|twin|full|queen|left|right|both|none|color|colour',re.I)
        clicked=set()
        for _pass in range(3):
            changed=0
            for fr in page.frames:
                try:
                    els=await fr.evaluate("""
                    ()=>[...document.querySelectorAll('button,[role=button],[role=radio],label,input[type=radio],input[type=checkbox]')].map((el,i)=>({i,text:(el.innerText||el.textContent||el.getAttribute('aria-label')||el.value||el.id||'').trim().replace(/\\s+/g,' ').slice(0,180)}))
                    """)
                    loc=fr.locator('button,[role=button],[role=radio],label,input[type=radio],input[type=checkbox]')
                    for e in els:
                        txt=e['text']; key=f'{fr.url}|{e["i"]}|{txt}'
                        if key in clicked or not kw.search(txt) or re.search(r'build your own|customize a best seller|add to cart|checkout|view cart',txt,re.I): continue
                        clicked.add(key)
                        try:
                            await loc.nth(e['i']).click(force=True,timeout=1200); changed+=1; await page.wait_for_timeout(650)
                        except: pass
                except: pass
            print('PASS',_pass+1,'changed',changed,'assets',len(seen_hash),flush=True)
            if not changed: break
        await page.wait_for_timeout(5000)
        await dump(page)
        if tasks: await asyncio.gather(*list(tasks),return_exceptions=True)

        # de-dupe manifest aliases
        out=[]; seen=set()
        for r in manifest:
            k=(r['url'],r['file'])
            if k not in seen: seen.add(k); out.append(r)
        diag['unique_assets']=len(seen_hash)
        diag['manifest_rows']=len(out)
        diag['bunny_cdn_assets']=sum('murphy-beds-models.b-cdn.net' in r['url'] for r in out)
        diag['glb_assets']=sum(r['file'].endswith('.glb') for r in out)
        diag['image_assets']=sum(r['content_type'].startswith('image/') for r in out)
        (OUT/'manifest.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
        (OUT/'network.json').write_text(json.dumps(network,indent=2),encoding='utf-8')
        (OUT/'urls.txt').write_text('\n'.join(sorted({r['url'] for r in out}))+'\n',encoding='utf-8')
        (OUT/'diagnostics.json').write_text(json.dumps(diag,indent=2),encoding='utf-8')
        print(json.dumps(diag,indent=2),flush=True)
        await browser.close()

if __name__=='__main__': asyncio.run(main())
