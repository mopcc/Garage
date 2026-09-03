const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const https = require('https');
const http = require('http');

const BUNDLE='https://www.murphybeds.com/cdn/shop/t/6/assets/bundle.js?v=507504753671215351787248627';
const OUT=path.resolve('murphybeds-configurator-direct');
const DIR=path.join(OUT,'assets');
fs.mkdirSync(DIR,{recursive:true});

const finishes=['black','burma-cherry','celadon','charcoal','dosha','ecru','eucalyptus','european-walnut','international-white','misty-white','mysterious-blue','pelee-island-pear','quarry','sable-glow','unfinished-maple','white-smoke'];
const textureBase='https://murphy-beds-models.b-cdn.net/bed-finishes';

function get(url){return new Promise((resolve,reject)=>{const lib=url.startsWith('https:')?https:http; const req=lib.get(url,{headers:{'User-Agent':'Mozilla/5.0','Accept':'*/*'}},res=>{if(res.statusCode>=300&&res.statusCode<400&&res.headers.location){res.resume();return resolve(get(new URL(res.headers.location,url).href));}const chunks=[];res.on('data',d=>chunks.push(d));res.on('end',()=>resolve({status:res.statusCode,headers:res.headers,body:Buffer.concat(chunks)}));});req.setTimeout(30000,()=>req.destroy(new Error('timeout')));req.on('error',reject);});}
function safe(u){return u.replace(/^https?:\/\//,'').replace(/[?#].*$/,'').replace(/[^a-zA-Z0-9._-]+/g,'_').slice(-190)}
function hash(b){return crypto.createHash('sha256').update(b).digest('hex')}
function ext(u,ct=''){let e=path.extname(new URL(u).pathname);if(/^\.(png|jpe?g|webp|gif|svg|avif)$/i.test(e)) return e; if(ct.includes('png'))return'.png'; if(ct.includes('jpeg'))return'.jpg'; if(ct.includes('svg'))return'.svg'; if(ct.includes('webp'))return'.webp'; return '.bin';}

(async()=>{
 const br=await get(BUNDLE); if(br.status!==200) throw new Error('bundle '+br.status); const js=br.body.toString('utf8'); fs.writeFileSync(path.join(OUT,'bundle.js'),js);
 const urls=new Set();
 // Every literal image URL embedded in the configurator bundle itself.
 for(const m of js.matchAll(/https?:\\?\/\\?\/[^"'`\\\s)]+\.(?:png|jpe?g|webp|gif|svg|avif)(?:\?[^"'`\\\s)]*)?/ig)) urls.add(m[0].replace(/\\\//g,'/'));
 // The Three.js configurator generates finish texture URLs dynamically; enumerate all known finish handles and texture maps.
 for(const f of finishes){urls.add(`${textureBase}/${f}.jpg`);urls.add(`${textureBase}/${f}_roughness.jpg`);urls.add(`${textureBase}/${f}_normal.jpg`)}
 urls.add(`${textureBase}/oak-stripped_gloss.jpg`); urls.add(`${textureBase}/oak-stripped_detailrough.jpg`);
 // Keep this package configurator-only, explicitly excluding bestseller imagery/page merchandising.
 const candidates=[...urls].filter(u=>!/(recommended-build|favicon|checkout-logo|affirm|googleadservices|facebook|pinterest)/i.test(u));
 const seen=new Map(), manifest=[], missing=[];
 let i=0;
 for(const u of candidates){i++; try{const r=await get(u); if(r.status!==200||r.body.length<20){missing.push({url:u,status:r.status});continue;} const ct=(r.headers['content-type']||'').toLowerCase(); if(!(/image\//.test(ct)||/\.(png|jpe?g|webp|gif|svg|avif)(?:[?#]|$)/i.test(u))){continue;} const h=hash(r.body); if(seen.has(h)){manifest.push({url:u,duplicateOf:seen.get(h),sha256:h,bytes:r.body.length});continue;} const file=`${String(seen.size+1).padStart(4,'0')}-${h.slice(0,10)}-${safe(u)}${path.extname(safe(u))?'':ext(u,ct)}`; fs.writeFileSync(path.join(DIR,file),r.body); seen.set(h,file); manifest.push({url:u,file,sha256:h,bytes:r.body.length,contentType:ct}); console.log(i+'/'+candidates.length,file); }catch(e){missing.push({url:u,error:String(e.message||e)});}}
 fs.writeFileSync(path.join(OUT,'manifest.json'),JSON.stringify(manifest,null,2));
 fs.writeFileSync(path.join(OUT,'urls.txt'),manifest.map(x=>x.url).join('\n')+'\n');
 fs.writeFileSync(path.join(OUT,'missing.json'),JSON.stringify(missing,null,2));
 fs.writeFileSync(path.join(OUT,'summary.json'),JSON.stringify({bundle:BUNDLE,candidateUrls:candidates.length,successfulUrls:manifest.length,uniqueFiles:seen.size,missing:missing.length,totalUniqueBytes:[...seen.values()].reduce((n,f)=>n+fs.statSync(path.join(DIR,f)).size,0),finishes},null,2));
 console.log('SUMMARY',fs.readFileSync(path.join(OUT,'summary.json'),'utf8'));
})().catch(e=>{console.error(e);process.exit(1)});
