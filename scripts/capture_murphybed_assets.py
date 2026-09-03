import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

TARGET = os.environ.get("TARGET_URL", "https://www.murphybeds.com/pages/buildabed")
OUT = Path(os.environ.get("OUTPUT_DIR", "murphybed_assets"))
ASSET_DIR = OUT / "assets"
OUT.mkdir(parents=True, exist_ok=True)
ASSET_DIR.mkdir(parents=True, exist_ok=True)

saved_hashes = {}
manifest = []
network_log = []
active_tasks = set()

ASSET_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".svg", ".glb", ".gltf", ".hdr", ".bin"}
CONTENT_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/avif": ".avif",
    "image/gif": ".gif", "image/svg+xml": ".svg", "model/gltf-binary": ".glb", "model/gltf+json": ".gltf",
    "application/octet-stream": ".bin"
}


def ext_for(url, content_type):
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in ASSET_EXTS:
        return suffix
    return CONTENT_EXT.get((content_type or "").split(";")[0].strip().lower(), ".bin")


def wanted(url, ctype):
    suffix = Path(urlparse(url).path).suffix.lower()
    ctype = (ctype or "").lower()
    return ctype.startswith("image/") or ctype.startswith("model/") or suffix in ASSET_EXTS or "murphy-beds-models.b-cdn.net" in url


async def save_bytes(url, body, content_type, status=None, source="network"):
    if not body:
        return
    digest = hashlib.sha256(body).hexdigest()
    ext = ext_for(url, content_type)
    if digest in saved_hashes:
        filename = saved_hashes[digest]
        duplicate = True
    else:
        filename = f"{digest[:20]}{ext}"
        (ASSET_DIR / filename).write_bytes(body)
        saved_hashes[digest] = filename
        duplicate = False
    manifest.append({
        "url": url, "file": f"assets/{filename}", "sha256": digest, "bytes": len(body),
        "content_type": content_type, "status": status, "source": source, "duplicate_content": duplicate,
    })


async def handle_response(response):
    try:
        ctype = (response.headers.get("content-type") or "").lower()
        network_log.append({"url": response.url, "status": response.status, "content_type": ctype})
        if wanted(response.url, ctype):
            try:
                await save_bytes(response.url, await response.body(), ctype, response.status, "network")
            except Exception as exc:
                network_log.append({"url": response.url, "error": f"body: {exc}"})
    except Exception as exc:
        network_log.append({"url": getattr(response, "url", "unknown"), "error": str(exc)})


async def request_and_save(context, url, source="dom"):
    if not url or url.startswith(("data:", "blob:")):
        return
    try:
        r = await context.request.get(url, timeout=25000)
        ctype = (r.headers.get("content-type") or "").lower()
        if r.ok and wanted(url, ctype):
            await save_bytes(url, await r.body(), ctype, r.status, source)
    except Exception:
        pass


async def collect_dom_assets(page, context):
    urls = set()
    for frame in page.frames:
        try:
            urls.update(await frame.evaluate("""
            () => {
              const out = new Set();
              for (const el of document.querySelectorAll('img,source')) {
                if (el.currentSrc) out.add(el.currentSrc);
                if (el.src) out.add(el.src);
                for (const p of (el.srcset || '').split(',')) { const u=p.trim().split(/\\s+/)[0]; if(u) out.add(u); }
              }
              for (const el of document.querySelectorAll('*')) {
                const bg = getComputedStyle(el).backgroundImage || '';
                for (const m of bg.matchAll(/url\\([\"']?(.*?)[\"']?\\)/g)) if(m[1]) out.add(new URL(m[1], location.href).href);
              }
              for (const e of performance.getEntriesByType('resource')) out.add(e.name);
              return [...out];
            }
            """))
        except Exception:
            pass
    await asyncio.gather(*(request_and_save(context, u) for u in urls), return_exceptions=True)


async def dump_page(page):
    try:
        (OUT / "page_text.txt").write_text(await page.locator("body").inner_text(timeout=5000), encoding="utf-8")
    except Exception:
        pass
    controls = []
    for fi, frame in enumerate(page.frames):
        try:
            rows = await frame.evaluate("""
            () => [...document.querySelector('body').querySelectorAll('button,a,input,label,select,[role=button],[role=radio],[role=option],[tabindex]')]
              .map((el,i)=>({
                i, tag:el.tagName.toLowerCase(), type:(el.type||''), text:(el.innerText||el.textContent||'').trim().replace(/\\s+/g,' ').slice(0,220),
                aria:el.getAttribute('aria-label')||'', title:el.getAttribute('title')||'', name:el.getAttribute('name')||'', value:el.value||'',
                id:el.id||'', cls:(el.className||'').toString().slice(0,220), href:el.href||'', role:el.getAttribute('role')||''
              }))
            """)
            for r in rows:
                r["frame"] = fi
                r["frame_url"] = frame.url
                controls.append(r)
        except Exception:
            pass
    (OUT / "controls.json").write_text(json.dumps(controls, indent=2), encoding="utf-8")


async def click_exact_text(page, text):
    did = 0
    for frame in page.frames:
        try:
            # Prefer interactive elements, then labels/spans whose closest interactive ancestor can be clicked.
            result = await frame.evaluate("""
            (needle) => {
              const norm = s => (s||'').trim().replace(/\\s+/g,' ').toLowerCase();
              const hits = [...document.querySelectorAll('button,[role=button],[role=radio],label,a,span,div')]
                .filter(el => norm(el.textContent) === norm(needle));
              let count=0;
              for (const el of hits) {
                const t = el.closest('button,[role=button],[role=radio],label') || el;
                const href = t.href || '';
                if (href && /pages\\/buildabed/i.test(href)) continue;
                try { t.click(); count++; } catch(e) {}
              }
              return count;
            }
            """, text)
            did += int(result or 0)
        except Exception:
            pass
    if did:
        print(f"clicked text {text!r}: {did}")
        await page.wait_for_timeout(1400)
    return did


async def exercise_known_options(page, context):
    # These are the visible option labels used by the Build Your Own configurator.
    option_groups = [
        ["Vertical", "Horizontal"],
        ["Twin", "Full", "Queen"],
        ["None", "Left", "Right", "Both"],
        ["Open", "Closed"],
    ]
    for group in option_groups:
        for text in group:
            await click_exact_text(page, text)
            await collect_dom_assets(page, context)

    # Click configurator-specific interactive controls discovered by keywords, avoiding links/navigation.
    keywords = re.compile(r"cabinet|finish|door|side|width|open|closed|vertical|horizontal|twin|full|queen|left|right|both|none|color|colour", re.I)
    clicked = set()
    for pass_no in range(6):
        changed = 0
        for frame in page.frames:
            try:
                rows = await frame.evaluate("""
                () => [...document.querySelectorAll('button,[role=button],[role=radio],label,input[type=radio],input[type=checkbox]')]
                 .map((el,i)=>({i, text:(el.innerText||el.textContent||el.getAttribute('aria-label')||el.value||el.id||'').trim().replace(/\\s+/g,' ').slice(0,180)}))
                """)
                loc = frame.locator('button,[role=button],[role=radio],label,input[type=radio],input[type=checkbox]')
                for row in rows:
                    text = row.get("text", "")
                    key = f"{frame.url}|{row['i']}|{text}"
                    if key in clicked or not keywords.search(text):
                        continue
                    if re.search(r"build your own|customize a best seller|add to cart|checkout|view cart", text, re.I):
                        continue
                    clicked.add(key)
                    try:
                        await loc.nth(row["i"]).click(force=True, timeout=1600)
                        changed += 1
                        await page.wait_for_timeout(800)
                    except Exception:
                        pass
            except Exception:
                pass
        await collect_dom_assets(page, context)
        print(f"pass {pass_no+1}: {changed} keyword controls clicked; {len(saved_hashes)} unique assets")
        if changed == 0:
            break


async def write_outputs(diag):
    dedup=[]; seen=set()
    for r in manifest:
        k=(r['url'],r['file'],r['source'])
        if k not in seen: seen.add(k); dedup.append(r)
    diag["unique_assets"] = len(saved_hashes)
    diag["manifest_rows"] = len(dedup)
    diag["model_assets"] = sum(1 for r in dedup if r['file'].endswith(('.glb','.gltf')))
    diag["image_assets"] = sum(1 for r in dedup if r.get('content_type','').startswith('image/'))
    (OUT/"manifest.json").write_text(json.dumps(dedup,indent=2),encoding="utf-8")
    (OUT/"network.json").write_text(json.dumps(network_log,indent=2),encoding="utf-8")
    (OUT/"diagnostics.json").write_text(json.dumps(diag,indent=2),encoding="utf-8")
    (OUT/"urls.txt").write_text("\n".join(sorted({r['url'] for r in dedup}))+"\n",encoding="utf-8")


async def main():
    diag={"target":TARGET}
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=["--disable-dev-shm-usage","--no-sandbox"])
        context=await browser.new_context(viewport={"width":1800,"height":1400}, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152 Safari/537.36")
        page=await context.new_page()
        def on_resp(resp):
            t=asyncio.create_task(handle_response(resp)); active_tasks.add(t); t.add_done_callback(active_tasks.discard)
        page.on("response",on_resp)
        try:
            await page.goto(TARGET,wait_until="domcontentloaded",timeout=60000)
            await page.wait_for_timeout(9000)
            diag["title"]=await page.title(); diag["url"]=page.url
            await dump_page(page)
            await collect_dom_assets(page,context)
            await exercise_known_options(page,context)
            await page.wait_for_timeout(4000)
            await dump_page(page)
            await collect_dom_assets(page,context)
            if active_tasks: await asyncio.gather(*list(active_tasks),return_exceptions=True)
        except Exception as exc:
            diag["error"]=repr(exc)
            if active_tasks: await asyncio.gather(*list(active_tasks),return_exceptions=True)
        await write_outputs(diag)
        print(json.dumps(diag,indent=2))
        await browser.close()

if __name__=="__main__": asyncio.run(main())
