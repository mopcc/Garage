import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

TARGET = os.environ.get("TARGET_URL", "https://www.murphybeds.com/pages/buildabed")
OUT = Path(os.environ.get("OUTPUT_DIR", "murphybed_assets"))
IMG_DIR = OUT / "images"
OUT.mkdir(parents=True, exist_ok=True)
IMG_DIR.mkdir(parents=True, exist_ok=True)

capture_active = False
saved_hashes = {}
manifest = []
network_log = []
active_tasks = set()

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".svg"}
CONTENT_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}
SKIP_TEXT = re.compile(r"checkout|view cart|continue shopping|contact|privacy|returns|support|help|shop murphy|hardware kits|commercial|comparison chart|choose this build|add to cart|currency", re.I)
CONFIG_TEXT = re.compile(r"build your own|vertical|horizontal|twin|full|queen|king|cabinet|finish|door|side|left|right|both|width|open|closed|toggle|desk|shelf|leg|hardware|orientation|size|style|color|colour|next|back|previous|continue", re.I)


def ext_for(url, content_type):
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in IMAGE_EXTS:
        return suffix
    return CONTENT_EXT.get((content_type or "").split(";")[0].strip().lower(), ".bin")


def is_image_url(url):
    return Path(urlparse(url).path).suffix.lower() in IMAGE_EXTS


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
        (IMG_DIR / filename).write_bytes(body)
        saved_hashes[digest] = filename
        duplicate = False
    manifest.append({
        "url": url,
        "file": f"images/{filename}",
        "sha256": digest,
        "bytes": len(body),
        "content_type": content_type,
        "status": status,
        "source": source,
        "duplicate_content": duplicate,
    })


async def handle_response(response):
    global capture_active
    try:
        ctype = (response.headers.get("content-type") or "").lower()
        network_log.append({
            "url": response.url,
            "status": response.status,
            "content_type": ctype,
            "capture_active": capture_active,
        })
        if not capture_active:
            return
        if ctype.startswith("image/") or is_image_url(response.url):
            try:
                body = await response.body()
                await save_bytes(response.url, body, ctype, response.status, "network")
            except Exception as exc:
                network_log.append({"url": response.url, "error": f"body: {exc}"})
    except Exception as exc:
        network_log.append({"url": getattr(response, "url", "unknown"), "error": str(exc)})


async def request_and_save(context, url, source="dom-discovery"):
    if not url or url.startswith(("data:", "blob:")):
        return
    try:
        r = await context.request.get(url, timeout=25000)
        if not r.ok:
            return
        ctype = (r.headers.get("content-type") or "").lower()
        if not (ctype.startswith("image/") or is_image_url(url)):
            return
        body = await r.body()
        await save_bytes(url, body, ctype, r.status, source)
    except Exception as exc:
        network_log.append({"url": url, "error": f"request: {exc}"})


async def collect_dom_asset_urls(page, context):
    urls = set()
    for frame in page.frames:
        try:
            found = await frame.evaluate("""
            () => {
              const out = new Set();
              for (const el of document.querySelectorAll('img,source')) {
                if (el.currentSrc) out.add(el.currentSrc);
                if (el.src) out.add(el.src);
                const ss = el.srcset || '';
                for (const part of ss.split(',')) {
                  const u = part.trim().split(/\\s+/)[0];
                  if (u) out.add(u);
                }
              }
              for (const el of document.querySelectorAll('*')) {
                const bg = getComputedStyle(el).backgroundImage || '';
                for (const m of bg.matchAll(/url\\([\"']?(.*?)[\"']?\\)/g)) {
                  if (m[1]) out.add(new URL(m[1], location.href).href);
                }
              }
              for (const e of performance.getEntriesByType('resource')) {
                const u = e.name || '';
                if (/\\.(png|jpe?g|webp|avif|gif|svg)(\\?|$)/i.test(u)) out.add(u);
              }
              return [...out];
            }
            """)
            urls.update(found)
        except Exception:
            pass
    await asyncio.gather(*(request_and_save(context, u) for u in urls), return_exceptions=True)
    return urls


async def dismiss_noise(page):
    for label in ["Accept", "Accept all", "Allow all", "Close", "No thanks", "Dismiss"]:
        try:
            loc = page.get_by_role("button", name=re.compile(f"^{re.escape(label)}$", re.I))
            if await loc.count():
                await loc.first.click(timeout=1200)
        except Exception:
            pass


async def enter_build_your_own(page):
    global capture_active
    candidates = []
    for frame in page.frames:
        try:
            loc = frame.get_by_text(re.compile(r"^\s*Build your own\s*$", re.I))
            n = await loc.count()
            for i in range(n):
                candidates.append(loc.nth(i))
        except Exception:
            pass
    if not candidates:
        raise RuntimeError("Could not find the 'Build your own' control")

    capture_active = True
    last_error = None
    for loc in reversed(candidates):
        try:
            await loc.scroll_into_view_if_needed(timeout=3000)
            await loc.click(timeout=5000)
            await page.wait_for_timeout(4000)
            try:
                await page.wait_for_load_state("networkidle", timeout=12000)
            except PlaywrightTimeoutError:
                pass
            return
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"Found 'Build your own' but could not click it: {last_error}")


async def inspect_controls(frame):
    try:
        return await frame.evaluate("""
        () => [...document.querySelectorAll('input,select,button,[role=button],label')]
          .filter(el => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
          })
          .map((el, i) => ({
            i,
            tag: el.tagName.toLowerCase(),
            type: (el.type || '').toLowerCase(),
            text: (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 180),
            aria: (el.getAttribute('aria-label') || '').trim(),
            title: (el.getAttribute('title') || '').trim(),
            name: (el.getAttribute('name') || '').trim(),
            value: (el.value || '').toString().trim().slice(0, 120),
            id: (el.id || '').trim(),
            disabled: !!el.disabled
          }))
        """)
    except Exception:
        return []


async def click_element_by_index(frame, idx):
    locator = frame.locator('input,select,button,[role=button],label').nth(idx)
    try:
        await locator.scroll_into_view_if_needed(timeout=1500)
    except Exception:
        pass
    await locator.click(timeout=2500, force=True)


async def exercise_configurator(page, context):
    clicked = set()
    select_done = set()
    base_host = urlparse(page.url).netloc
    safe_url = page.url

    for pass_no in range(12):
        changed = 0
        for frame in list(page.frames):
            controls = await inspect_controls(frame)
            for c in controls:
                if c.get("disabled"):
                    continue
                sig = "|".join(str(c.get(k, "")) for k in ("tag", "type", "text", "aria", "title", "name", "value", "id"))
                key = f"{frame.url}|{sig}"
                text = " ".join(str(c.get(k, "")) for k in ("text", "aria", "title", "name", "value", "id"))

                if c["tag"] == "select":
                    if key in select_done:
                        continue
                    select_done.add(key)
                    try:
                        sel = frame.locator('input,select,button,[role=button],label').nth(c["i"])
                        options = await sel.locator('option').evaluate_all("els => els.map(e => ({v:e.value, d:e.disabled}))")
                        for opt in options:
                            if opt["d"] or not opt["v"]:
                                continue
                            try:
                                await sel.select_option(opt["v"], timeout=2500)
                                changed += 1
                                await page.wait_for_timeout(500)
                            except Exception:
                                pass
                    except Exception:
                        pass
                    continue

                unconditional = c["tag"] == "input" and c["type"] in {"radio", "checkbox"}
                label_with_control = c["tag"] == "label" and bool(c.get("text") or c.get("aria") or c.get("title"))
                keyword_match = bool(CONFIG_TEXT.search(text))
                if not (unconditional or label_with_control or keyword_match):
                    continue
                if SKIP_TEXT.search(text):
                    continue
                if key in clicked:
                    continue
                clicked.add(key)

                try:
                    before = page.url
                    await click_element_by_index(frame, c["i"])
                    changed += 1
                    await page.wait_for_timeout(650)
                    now = page.url
                    bad_nav = urlparse(now).netloc != base_host or re.search(r"/(cart|checkout|products|collections)(/|$)", urlparse(now).path, re.I)
                    if bad_nav:
                        await page.goto(safe_url, wait_until="domcontentloaded", timeout=45000)
                        await page.wait_for_timeout(2500)
                    elif now != before:
                        safe_url = now
                except Exception:
                    pass

        await collect_dom_asset_urls(page, context)
        print(f"interaction pass {pass_no + 1}: {changed} controls exercised; {len(saved_hashes)} unique images")
        if changed == 0 and pass_no >= 2:
            break


async def main():
    diagnostics = {"target": TARGET}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"])
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1100},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152 Safari/537.36",
        )
        page = await context.new_page()

        def on_response(resp):
            task = asyncio.create_task(handle_response(resp))
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)

        page.on("response", on_response)
        await page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(5000)
        await dismiss_noise(page)
        diagnostics["initial_url"] = page.url
        diagnostics["initial_title"] = await page.title()

        await enter_build_your_own(page)
        diagnostics["config_url"] = page.url
        diagnostics["config_title"] = await page.title()
        await collect_dom_asset_urls(page, context)
        await exercise_configurator(page, context)
        await page.wait_for_timeout(3500)
        await collect_dom_asset_urls(page, context)

        if active_tasks:
            await asyncio.gather(*list(active_tasks), return_exceptions=True)

        # De-dupe manifest rows by (url,file,source) while preserving all URL aliases.
        deduped = []
        seen_rows = set()
        for row in manifest:
            k = (row["url"], row["file"], row["source"])
            if k not in seen_rows:
                seen_rows.add(k)
                deduped.append(row)

        diagnostics["unique_images"] = len(saved_hashes)
        diagnostics["manifest_rows"] = len(deduped)
        (OUT / "manifest.json").write_text(json.dumps(deduped, indent=2), encoding="utf-8")
        (OUT / "network.json").write_text(json.dumps(network_log, indent=2), encoding="utf-8")
        (OUT / "diagnostics.json").write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")
        (OUT / "urls.txt").write_text("\n".join(sorted({r["url"] for r in deduped})) + "\n", encoding="utf-8")
        print(json.dumps(diagnostics, indent=2))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
