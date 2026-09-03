const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const START_URL = 'https://www.murphybeds.com/pages/buildabed';
const OUT = path.resolve(process.env.OUT_DIR || 'murphybeds-configurator-capture');
const ASSETS = path.join(OUT, 'assets');
const TEXT = path.join(OUT, 'network-text');
const SHOTS = path.join(OUT, 'screenshots');
for (const d of [OUT, ASSETS, TEXT, SHOTS]) fs.mkdirSync(d, { recursive: true });

const sleep = ms => new Promise(r => setTimeout(r, ms));
const sha256 = b => crypto.createHash('sha256').update(b).digest('hex');
const safe = s => (s || 'asset').replace(/^https?:\/\//, '').replace(/[?#].*$/, '').replace(/[^a-zA-Z0-9._-]+/g, '_').slice(-180) || 'asset';
const imageExt = ct => ct.includes('png') ? '.png' : ct.includes('jpeg') ? '.jpg' : ct.includes('webp') ? '.webp' : ct.includes('avif') ? '.avif' : ct.includes('svg') ? '.svg' : ct.includes('gif') ? '.gif' : ct.includes('bmp') ? '.bmp' : '';
const isImage = (url, ct='') => /image\//i.test(ct) || /\.(png|jpe?g|webp|avif|gif|svg|bmp)(?:[?#]|$)/i.test(url);
const isText = (url, ct='') => /(javascript|json|text\/|xml|css)/i.test(ct) || /\.(js|json|css)(?:[?#]|$)/i.test(url);
const likelyConfigurator = url => /(build.?a.?bed|configur|content\/build|wall.?bed|murphy|cdn\.shopify|shopifycdn)/i.test(url);

const allResponses = [];
const baselineImageUrls = new Set();
const builderImageUrls = new Set();
const textBodies = [];
const downloadedByHash = new Map();
let phase = 'baseline';

function writeJson(name, obj) {
  fs.writeFileSync(path.join(OUT, name), JSON.stringify(obj, null, 2));
}

async function saveImageBuffer(url, body, contentType, source='network') {
  if (!body || body.length < 20) return null;
  const h = sha256(body);
  if (downloadedByHash.has(h)) return downloadedByHash.get(h);
  let ext = path.extname(new URL(url).pathname);
  if (!/^\.(png|jpe?g|webp|avif|gif|svg|bmp)$/i.test(ext)) ext = imageExt(contentType) || '.bin';
  const file = `${String(downloadedByHash.size + 1).padStart(5,'0')}-${h.slice(0,12)}-${safe(url)}${path.extname(safe(url)) ? '' : ext}`;
  const full = path.join(ASSETS, file);
  fs.writeFileSync(full, body);
  const rec = { file, url, sha256: h, bytes: body.length, contentType, source };
  downloadedByHash.set(h, rec);
  return rec;
}

async function fetchAndSave(context, url, source='discovered') {
  try {
    const r = await context.request.get(url, { timeout: 30000, failOnStatusCode: false });
    if (!r.ok()) return null;
    const ct = (r.headers()['content-type'] || '').toLowerCase();
    const body = await r.body();
    if (isImage(url, ct)) return await saveImageBuffer(url, body, ct, source);
  } catch (_) {}
  return null;
}

function extractUrls(text, baseUrl) {
  const out = new Set();
  const patterns = [
    /https?:\\?\/\\?\/[^\s"'<>\\)]+?\.(?:png|jpe?g|webp|avif|gif|svg|bmp)(?:\?[^\s"'<>\\)]*)?/ig,
    /["']([^"']+?\.(?:png|jpe?g|webp|avif|gif|svg|bmp)(?:\?[^"']*)?)["']/ig,
    /url\((?:["']?)([^)"']+\.(?:png|jpe?g|webp|avif|gif|svg|bmp)(?:\?[^)"']*)?)/ig
  ];
  for (const re of patterns) {
    let m;
    while ((m = re.exec(text))) {
      let u = (m[1] || m[0]).replace(/\\\//g, '/').replace(/^['"\s]+|['"\s]+$/g, '');
      try { out.add(new URL(u, baseUrl).href); } catch (_) {}
    }
  }
  return [...out];
}

async function findBuilderRoot(page) {
  return await page.evaluateHandle(() => {
    const els = [...document.querySelectorAll('section,main,div,form')];
    const scored = els.map(el => {
      const t = (el.innerText || '').toLowerCase();
      let score = 0;
      for (const k of ['wall bed type','wall bed size','cabinet finish','door finish','side cabinet']) if (t.includes(k)) score += 10;
      if (t.includes('build your own')) score += 5;
      if (t.includes('customize a bestseller')) score -= 20;
      return { el, score, len: t.length };
    }).filter(x => x.score > 0).sort((a,b) => b.score-a.score || a.len-b.len);
    return scored[0]?.el || document.querySelector('main') || document.body;
  });
}

async function builderSignature(page) {
  return page.evaluate(() => {
    const text = document.body.innerText || '';
    const start = Math.max(0, text.toLowerCase().indexOf('build your own'));
    return text.slice(start, start + 7000).replace(/\s+/g,' ').trim();
  });
}

async function clickBuildYourOwn(page) {
  const candidates = [
    page.getByRole('link', { name: /build your own/i }),
    page.getByRole('button', { name: /build your own/i }),
    page.getByText(/build your own/i, { exact: true })
  ];
  for (const loc of candidates) {
    try {
      if (await loc.count()) {
        await loc.last().scrollIntoViewIfNeeded();
        await loc.last().click({ timeout: 8000 });
        await page.waitForLoadState('domcontentloaded', { timeout: 15000 }).catch(()=>{});
        await sleep(3500);
        return true;
      }
    } catch (_) {}
  }
  return false;
}

async function sweepConfigurator(page) {
  const visited = new Set();
  const screenshots = [];
  let stagnant = 0;
  for (let pass=0; pass<80 && stagnant<10; pass++) {
    const root = await findBuilderRoot(page);
    const controls = await root.evaluate(el => {
      const nodes = [...el.querySelectorAll('button,[role=button],label,input[type=radio],input[type=checkbox],select,a')];
      return nodes.map((n,i) => {
        const r = n.getBoundingClientRect();
        const st = getComputedStyle(n);
        const visible = r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
        const text = (n.innerText || n.getAttribute('aria-label') || n.getAttribute('title') || n.value || n.alt || '').trim();
        const name = n.getAttribute('name') || '';
        const value = n.value || n.getAttribute('data-value') || '';
        const type = n.tagName.toLowerCase() + ':' + (n.getAttribute('type') || n.getAttribute('role') || '');
        const id = n.id || '';
        return { i, visible, text, name, value, type, id };
      }).filter(x => x.visible);
    });

    let acted = false;
    for (const c of controls) {
      const key = `${c.type}|${c.name}|${c.value}|${c.text}|${c.id}`.slice(0,500);
      if (visited.has(key)) continue;
      const s = `${c.text} ${c.name} ${c.value}`.toLowerCase();
      if (/add to cart|checkout|buy now|continue shopping|contact|phone|menu|currency|country|help|returns|privacy|choose this build|customize a bestseller/.test(s)) { visited.add(key); continue; }
      // Keep links conservative; buttons/labels/inputs/selects inside the builder are generally safe.
      if (c.type.startsWith('a:') && !/(open|closed|vertical|horizontal|twin|full|double|queen|cabinet|finish|door|side|left|right|both|none|width)/.test(s)) { visited.add(key); continue; }
      try {
        if (c.type.startsWith('select:')) {
          const sel = root.locator('select').nth(controls.filter(x=>x.type.startsWith('select:')).findIndex(x=>x.i===c.i));
          const opts = await sel.locator('option').evaluateAll(os => os.map(o => ({value:o.value,text:o.textContent.trim()})));
          for (const o of opts) {
            const ok = `${key}|option:${o.value}|${o.text}`;
            if (visited.has(ok)) continue;
            visited.add(ok);
            await sel.selectOption(o.value).catch(()=>{});
            await sleep(900);
          }
          visited.add(key); acted = true; break;
        }
        const node = root.locator('button,[role=button],label,input[type=radio],input[type=checkbox],select,a').nth(c.i);
        await node.scrollIntoViewIfNeeded().catch(()=>{});
        await node.click({ timeout: 5000, force: true }).catch(async()=>{
          if (c.id) await page.locator(`#${CSS.escape(c.id)}`).click({force:true,timeout:3000});
        });
        visited.add(key);
        acted = true;
        await sleep(1100);
        if (pass % 8 === 0) {
          const f = path.join(SHOTS, `sweep-${String(pass).padStart(3,'0')}.png`);
          await page.screenshot({ path:f, fullPage:true }).catch(()=>{});
          screenshots.push(path.basename(f));
        }
        break;
      } catch (_) { visited.add(key); }
    }
    if (acted) stagnant = 0; else { stagnant++; await sleep(600); }
  }
  return { visitedControls: visited.size, screenshots };
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1100 },
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36'
  });
  const page = await context.newPage();

  page.on('response', async res => {
    const url = res.url();
    const status = res.status();
    const headers = res.headers();
    const ct = (headers['content-type'] || '').toLowerCase();
    allResponses.push({ phase, url, status, contentType: ct });
    if (status < 200 || status >= 400) return;
    try {
      if (isImage(url, ct)) {
        if (phase === 'baseline') baselineImageUrls.add(url); else builderImageUrls.add(url);
        const body = await res.body();
        if (phase === 'builder' || likelyConfigurator(url)) await saveImageBuffer(url, body, ct, `network-${phase}`);
      } else if (isText(url, ct) && (phase === 'builder' || likelyConfigurator(url))) {
        const body = await res.body();
        if (body.length && body.length < 12_000_000) {
          const txt = body.toString('utf8');
          textBodies.push({ url, text: txt, contentType: ct });
          fs.writeFileSync(path.join(TEXT, `${String(textBodies.length).padStart(4,'0')}-${safe(url)}.txt`), txt);
        }
      }
    } catch (_) {}
  });

  console.log('Loading', START_URL);
  await page.goto(START_URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await sleep(5000);
  await page.screenshot({ path: path.join(SHOTS,'00-baseline.png'), fullPage:true }).catch(()=>{});
  const baselineDomImgs = await page.locator('img').evaluateAll(imgs => imgs.map(i => i.currentSrc || i.src).filter(Boolean)).catch(()=>[]);
  baselineDomImgs.forEach(u => baselineImageUrls.add(u));

  phase = 'builder';
  const clicked = await clickBuildYourOwn(page);
  console.log('Clicked Build your own:', clicked, 'URL:', page.url());
  await page.screenshot({ path: path.join(SHOTS,'01-builder.png'), fullPage:true }).catch(()=>{});
  const initialBuilderSignature = await builderSignature(page).catch(()=> '');
  fs.writeFileSync(path.join(OUT,'builder-visible-text.txt'), initialBuilderSignature);

  const sweep = await sweepConfigurator(page);
  console.log('Sweep complete', sweep);
  await sleep(2500);

  // Collect every image URL now visible in the builder DOM, including srcset/background-image.
  const domDiscovered = await page.evaluate(() => {
    const urls = new Set();
    for (const img of document.querySelectorAll('img,source')) {
      for (const a of ['src','currentSrc','srcset']) {
        const v = img[a] || img.getAttribute?.(a);
        if (!v) continue;
        for (const part of String(v).split(',')) urls.add(part.trim().split(/\s+/)[0]);
      }
    }
    for (const el of document.querySelectorAll('*')) {
      const bg = getComputedStyle(el).backgroundImage || '';
      for (const m of bg.matchAll(/url\(["']?([^"')]+)["']?\)/g)) urls.add(m[1]);
    }
    return [...urls].map(u => { try { return new URL(u, location.href).href; } catch { return null; } }).filter(Boolean);
  });
  for (const u of domDiscovered) {
    if (!baselineImageUrls.has(u) || likelyConfigurator(u)) await fetchAndSave(context, u, 'builder-dom');
  }

  // Extract image references from captured JS/JSON/CSS. This catches assets that the UI never lazily displayed.
  const discoveredFromText = new Set();
  for (const t of textBodies) for (const u of extractUrls(t.text, t.url)) discoveredFromText.add(u);
  console.log('Image URLs discovered in JS/JSON/CSS:', discoveredFromText.size);
  let n = 0;
  for (const u of discoveredFromText) {
    if (++n % 50 === 0) console.log('Fetching discovered image', n, '/', discoveredFromText.size);
    await fetchAndSave(context, u, 'js-json-css-reference');
  }

  const manifest = [...downloadedByHash.values()].sort((a,b)=>a.url.localeCompare(b.url));
  writeJson('asset-manifest.json', manifest);
  fs.writeFileSync(path.join(OUT,'asset-urls.txt'), manifest.map(x=>x.url).join('\n')+'\n');
  writeJson('network-responses.json', allResponses);
  writeJson('capture-summary.json', {
    startUrl: START_URL,
    finalUrl: page.url(),
    clickedBuildYourOwn: clicked,
    baselineImageUrlCount: baselineImageUrls.size,
    builderNetworkImageUrlCount: builderImageUrls.size,
    capturedTextResponseCount: textBodies.length,
    discoveredImageRefsInText: discoveredFromText.size,
    uniqueDownloadedAssets: manifest.length,
    uniqueDownloadedBytes: manifest.reduce((s,x)=>s+x.bytes,0),
    sweep
  });
  await browser.close();
  console.log(`DONE: ${manifest.length} unique configurator-related assets saved to ${OUT}`);
})().catch(err => { console.error(err); process.exit(1); });
