import os, re, json, math, time, sqlite3, logging, threading
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qs, unquote
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, render_template_string

app=Flask(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log=logging.getLogger("poketrack")
SCAN_LOCK=threading.Lock()
DB_PATH=os.getenv("DB_PATH","/data/poketrack.db")
ZIP=os.getenv("START_ZIP","22601")
RADIUS=int(os.getenv("RADIUS_MILES","80"))
UA=os.getenv("USER_AGENT","Mozilla/5.0 (compatible; PokeTrackRadar/1.0)")
HEADERS={"User-Agent":UA,"Accept-Language":"en-US,en;q=0.9"}
RETAILERS={
 "Target":["target.com"],"Walmart":["walmart.com"],"Best Buy":["bestbuy.com"],
 "Amazon":["amazon.com"],"Barnes & Noble":["barnesandnoble.com"],"Costco":["costco.com"],
 "Sam's Club":["samsclub.com"],"Walgreens":["walgreens.com"],"Dollar General":["dollargeneral.com"],
 "Hobby Lobby":["hobbylobby.com"],"Books-A-Million":["booksamillion.com"],"Gabe's":["gabesstores.com"]
}
SCHEMA="""
CREATE TABLE IF NOT EXISTS stores(id INTEGER PRIMARY KEY, retailer TEXT NOT NULL, name TEXT NOT NULL, address TEXT,
 city TEXT, state TEXT, postcode TEXT, lat REAL NOT NULL, lon REAL NOT NULL, distance REAL NOT NULL,
 source TEXT NOT NULL, source_url TEXT, checked_at TEXT NOT NULL, UNIQUE(retailer,name,lat,lon));
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, retailer TEXT NOT NULL, title TEXT NOT NULL,
 url TEXT NOT NULL UNIQUE, price TEXT, availability TEXT NOT NULL DEFAULT 'unknown',
 source TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS poketrack_scan_runs(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, status TEXT NOT NULL, found INTEGER DEFAULT 0, message TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS store_inventory(id INTEGER PRIMARY KEY,store_id INTEGER NOT NULL,product_url TEXT NOT NULL,product_title TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'unknown',quantity INTEGER,quantity_label TEXT NOT NULL DEFAULT 'count unavailable',source_url TEXT,detail TEXT,checked_at TEXT NOT NULL,UNIQUE(store_id,product_url));
CREATE TABLE IF NOT EXISTS store_inventory_checks(store_id INTEGER PRIMARY KEY,status TEXT NOT NULL,checked_at TEXT NOT NULL,detail TEXT);
"""
def now(): return datetime.now(timezone.utc).isoformat()
def conn():
 os.makedirs(os.path.dirname(DB_PATH) or ".",exist_ok=True)
 c=sqlite3.connect(DB_PATH,timeout=60); c.row_factory=sqlite3.Row; c.execute("PRAGMA busy_timeout=60000"); c.executescript(SCHEMA); c.commit(); return c
def haversine(a,b,c,d):
 r=3958.7613; p1,p2=math.radians(a),math.radians(c)
 return 2*r*math.asin(math.sqrt(math.sin((p2-p1)/2)**2+math.cos(p1)*math.cos(p2)*math.sin(math.radians(d-b)/2)**2))
def zip_origin(z):
 z=str(z).strip()
 if not re.fullmatch(r'\d{5}',z): raise RuntimeError('Enter a valid 5-digit US ZIP code')
 # ZIP-code endpoint; unlike the Census street-address endpoint, it accepts ZIP alone.
 try:
  r=requests.get('https://api.zippopotam.us/us/'+z,headers=HEADERS,timeout=20)
  r.raise_for_status(); places=r.json().get('places',[])
  if places:
   return float(places[0]['latitude']),float(places[0]['longitude'])
 except Exception as e:
  log.warning('ZIP lookup service failed for %s: %s; trying Census',z,e)
 # Census fallback using a city/state/ZIP query rather than ZIP-only address.
 r=requests.get('https://geocoding.geo.census.gov/geocoder/locations/onelineaddress',
  params={'address':'Winchester, Virginia '+z,'benchmark':'Public_AR_Current','format':'json'},headers=HEADERS,timeout=20)
 r.raise_for_status()
 matches=r.json().get('result',{}).get('addressMatches',[])
 if not matches: raise RuntimeError('ZIP '+z+' could not be geocoded by ZIP lookup or Census fallback')
 x=matches[0]['coordinates']; return float(x['y']),float(x['x'])
def discover_stores():
 # Import the researched branch list; no map service or Overpass request is used.
 import pathlib
 directory_path=pathlib.Path(__file__).with_name('store_directory.json')
 with directory_path.open(encoding='utf-8') as f: branches=json.load(f)
 origin_lat,origin_lon=zip_origin(ZIP)
 cache={}; saved=0; c=conn()
 # Remove obsolete Overpass-sourced map entries; preserve all product and scan history.
 c.execute("DELETE FROM stores WHERE source='OpenStreetMap'")
 for retailer,name,street,city,state,postcode,source_url in branches:
  try:
   if postcode not in cache:
    zr=requests.get('https://api.zippopotam.us/us/'+postcode,headers=HEADERS,timeout=15)
    zr.raise_for_status(); places=zr.json().get('places',[])
    if not places: continue
    cache[postcode]=(float(places[0]['latitude']),float(places[0]['longitude']))
   lat,lon=cache[postcode]; dist=haversine(origin_lat,origin_lon,lat,lon)
   if dist>RADIUS: continue
   address=f'{street}, {city}, {state} {postcode}'
   c.execute('''INSERT INTO stores(retailer,name,address,city,state,postcode,lat,lon,distance,source,source_url,checked_at)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(retailer,name,lat,lon) DO UPDATE SET address=excluded.address,
    city=excluded.city,state=excluded.state,postcode=excluded.postcode,distance=excluded.distance,
    source=excluded.source,source_url=excluded.source_url,checked_at=excluded.checked_at''',
    (retailer,name,address,city,state,postcode,lat,lon,round(dist,1),'Retailer web research',source_url,now()))
   saved+=1
  except Exception as e: log.warning('Skipping directory entry %s %s: %s',retailer,name,e)
 c.commit(); c.close(); return saved
def clean_url(u):
 u=unquote(u or "")
 if u.startswith("/url?"): u=parse_qs(urlparse(u).query).get("q",[""])[0]
 p=urlparse(u)
 if p.scheme not in ("http","https") or not p.netloc:return None
 if p.hostname and ("google." in p.hostname or "bing.com" in p.hostname):return None
 return urlunparse((p.scheme,p.netloc,p.path,"","",""))
def direct_catalog_url(retailer):
 # Public retailer catalog/search pages; no search-engine result pages or paid APIs.
 urls={
  "Target":"https://www.target.com/s?searchTerm=pokemon%20trading%20cards",
  "Walmart":"https://www.walmart.com/search?q=pokemon%20trading%20cards",
  "Walgreens":"https://www.walgreens.com/q/all-pokemon-cards",
  "Best Buy":"https://www.bestbuy.com/site/searchpage.jsp?st=pokemon+trading+cards",
  "Barnes & Noble":"https://www.barnesandnoble.com/s/pokemon%20trading%20cards",
  "Costco":"https://www.costco.com/CatalogSearch?keyword=pokemon",
  "Sam's Club":"https://www.samsclub.com/s/pokemon",
  "Dollar General":"https://www.dollargeneral.com/search?query=pokemon",
  "Hobby Lobby":"https://www.hobbylobby.com/search/?q=pokemon",
  "Books-A-Million":"https://www.booksamillion.com/search?query=pokemon",
  "Gabe's":"https://www.gabesstores.com/search?q=pokemon",
  "Amazon":"https://www.amazon.com/s?k=pokemon+trading+cards"
 }
 return urls.get(retailer)
def is_pokemon_tcg_title(title):
 title=(title or "").lower()
 if not re.search(r'pokemon|pokémon',title): return False
 if re.search(r'figure|plush|costume|apparel|backpack|toy car|lunch box',title): return False
 return bool(re.search(r'trading card|tcg|booster|elite trainer|etb|collection box|blister|tin|deck|card pack|card game',title))
def search_products(retailer):
 # Read retailer-owned catalog pages directly and follow their product links.
 catalog=direct_catalog_url(retailer)
 domain=RETAILERS[retailer][0]
 r=requests.get(catalog,headers=HEADERS,timeout=25)
 r.raise_for_status()
 soup=BeautifulSoup(r.text,"html.parser")
 found={}
 for a in soup.select("a[href]"):
  u=clean_url(requests.compat.urljoin(catalog,a.get("href","")))
  title=a.get_text(" ",strip=True)
  if not u or not (urlparse(u).hostname or "").lower().endswith(domain): continue
  if not title or not is_pokemon_tcg_title(title): continue
  found[u]=title[:250]
 # Some retailer catalogs expose product names in JSON-LD rather than anchor text.
 for node in soup.select('script[type="application/ld+json"]'):
  try:
   data=json.loads(node.string or node.get_text())
   stack=data if isinstance(data,list) else [data]
   while stack:
    obj=stack.pop()
    if isinstance(obj,dict):
     if isinstance(obj.get("@graph"),list): stack.extend(obj["@graph"])
     name=obj.get("name"); url=obj.get("url")
     if name and url and is_pokemon_tcg_title(name):
      u=clean_url(requests.compat.urljoin(catalog,url))
      if u and (urlparse(u).hostname or "").lower().endswith(domain): found[u]=str(name)[:250]
     stack.extend(v for v in obj.values() if isinstance(v,(dict,list)))
    elif isinstance(obj,list): stack.extend(obj)
  except Exception: pass
 products=[]; c=conn()
 for u,title in list(found.items())[:35]:
  price=None; stock="unknown"; source="retailer catalog page"
  try:
   page=requests.get(u,headers=HEADERS,timeout=18)
   if page.ok:
    ps=BeautifulSoup(page.text,"html.parser")
    og=ps.find("meta",property="og:title")
    if og and og.get("content"): title=og["content"][:250]
    for sel in ['meta[property="product:price:amount"]','meta[itemprop="price"]']:
     n=ps.select_one(sel)
     if n and n.get("content"): price=n["content"]; break
    source="retailer product page"
  except requests.RequestException as e:
   log.warning("Product page fetch failed for %s: %s",u,e)
  if not is_pokemon_tcg_title(title): continue
  t=now(); c.execute("""INSERT INTO products(retailer,title,url,price,availability,source,first_seen,last_seen)
   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,price=COALESCE(excluded.price,products.price),
   availability=excluded.availability,source=excluded.source,last_seen=excluded.last_seen""",(retailer,title,u,price,stock,source,t,t))
  products.append({"retailer":retailer,"title":title,"url":u,"price":price,"availability":stock})
 c.commit(); c.close(); return products
def branch_stock_status(blob):
 blob=(blob or "").lower()
 if re.search(r'out of stock|sold out|unavailable for pickup|not available at this store',blob): return "out_of_stock",None,"Out of stock"
 if re.search(r'in stock|available for pickup|pickup available|ready for pickup|limited stock|available now',blob):
  m=re.search(r'(?:only\s*)?(\d+)\s*(?:left|available|in stock)',blob)
  qty=int(m.group(1)) if m else None
  return "in_stock",qty,(str(qty) if qty is not None else "count unavailable")
 return "unknown",None,"count unavailable"
def branch_matches_page(store, page_text):
 text=(page_text or "").lower()
 postcode=str(store.get("postcode") or "").strip().lower()
 city=str(store.get("city") or "").strip().lower()
 name=str(store.get("name") or "").strip().lower()
 address=str(store.get("address") or "").strip().lower()
 # Require an exact branch identifier, not merely the same retailer or ZIP region.
 if postcode and postcode in text and city and city in text: return True
 if name and len(name)>5 and name in text: return True
 street=re.match(r'\s*(\d+)',address)
 if street and city and re.search(r'\b'+re.escape(street.group(1))+r'\b',text) and city in text: return True
 return False
def inventory_status_from_product_page(page_text):
 text=(page_text or "").lower()
 # Do not interpret shipping, delivery, or generic Add to Cart as local store stock.
 if re.search(r'not available at this store|not sold at your store|out of stock at this store|unavailable for pickup at this store',text):
  return "out_of_stock",None,"Out of stock"
 if re.search(r'in stock at this store|available for pickup at this store|pickup available at this store|in stock at \w[\w -]{0,45}store',text):
  m=re.search(r'(?:only\s*)?(\d+)\s*(?:left|available|in stock)',text)
  qty=int(m.group(1)) if m else None
  return "in_stock",qty,(str(qty) if qty is not None else "count unavailable")
 return "unknown",None,"count unavailable"
def scan_store_inventory():
 # Visit retailer-owned product pages directly. No Google/Bing snippets are used.
 c=conn()
 stores=[dict(x) for x in c.execute("SELECT * FROM stores ORDER BY retailer,distance")]
 products=[dict(x) for x in c.execute("SELECT * FROM products ORDER BY last_seen DESC")]
 by_retailer={}
 for p in products: by_retailer.setdefault(p["retailer"],[]).append(p)
 checked=0; confirmed=0; errors=0
 for store in stores:
  candidates=by_retailer.get(store["retailer"],[])
  matched=0; branch_hits=0
  if not candidates:
   c.execute("INSERT INTO store_inventory_checks(store_id,status,checked_at,detail) VALUES(?,?,?,?) ON CONFLICT(store_id) DO UPDATE SET status=excluded.status,checked_at=excluded.checked_at,detail=excluded.detail",
    (store["id"],"no_products",now(),"No Pokémon TCG product pages have been discovered on this retailer's website yet."))
   continue
  for product in candidates[:35]:
   try:
    response=requests.get(product["url"],headers=HEADERS,timeout=18)
    if not response.ok: continue
    page=BeautifulSoup(response.text,"html.parser")
    page_text=page.get_text(" ",strip=True)
    # Inspect embedded page state too; retailers often render inventory data in JSON.
    embedded=" ".join(x.get_text(" ",strip=True) for x in page.select("script[type='application/ld+json'],script#__NEXT_DATA__,script[type='application/json']"))
    combined=(page_text+" "+embedded)[:500000]
    matched+=1
    if not branch_matches_page(store,combined): continue
    branch_hits+=1
    status,qty,label=inventory_status_from_product_page(combined)
    if status=="unknown": continue
    checked_at=now()
    c.execute("""INSERT INTO store_inventory(store_id,product_url,product_title,status,quantity,quantity_label,source_url,detail,checked_at)
     VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(store_id,product_url) DO UPDATE SET product_title=excluded.product_title,status=excluded.status,quantity=excluded.quantity,quantity_label=excluded.quantity_label,source_url=excluded.source_url,detail=excluded.detail,checked_at=excluded.checked_at""",
     (store["id"],product["url"],product["title"],status,qty,label,product["url"],"Availability and branch identity both found on the retailer's own product page",checked_at))
    if status=="in_stock": confirmed+=1
   except requests.RequestException as e:
    errors+=1
    log.warning("Direct product-page check failed for %s at %s: %s",product["url"],store.get("name"),e)
  checked+=1
  state="checked" if branch_hits else ("error" if matched==0 else "no_match")
  detail=(f"{branch_hits} retailer product page(s) identified this exact branch; local stock wording was evaluated." if branch_hits
   else "Retailer product pages were opened directly, but none exposed this branch's identity and local stock together. Status remains unknown, not out of stock.")
  c.execute("INSERT INTO store_inventory_checks(store_id,status,checked_at,detail) VALUES(?,?,?,?) ON CONFLICT(store_id) DO UPDATE SET status=excluded.status,checked_at=excluded.checked_at,detail=excluded.detail",
   (store["id"],state,now(),detail))
  c.commit()
  time.sleep(0.1)
 c.close()
 return {"confirmed_products":confirmed,"message":f"Directly checked retailer product pages for {checked} branches; {confirmed} product/branch matches explicitly reported in stock. {errors} page requests failed. No search-engine snippets or guessed stock were used."}
def run_scan(kind):
 if not SCAN_LOCK.acquire(blocking=False):
  return {"status":"busy","message":"A scan is already running. Please wait for it to finish."},409
 try:
  c=conn(); cur=c.execute("INSERT INTO poketrack_scan_runs(kind,started_at,status) VALUES(?,?,?)",(kind,now(),"running")); rid=cur.lastrowid;c.commit();c.close()
 except Exception:
  SCAN_LOCK.release()
  raise
 total=0; notes=[]
 try:
  if kind in ("all","stores"):
   n=discover_stores();total+=n;notes.append(f"{n} physical store locations discovered")
  if kind in ("all","products"):
   for retailer in RETAILERS:
    try:
     n=len(search_products(retailer));total+=n;notes.append(f"{retailer}: {n} product pages")
    except Exception as e: notes.append(f"{retailer}: {str(e)[:100]}")
  if kind in ("all","inventory"):
   result=scan_store_inventory();total+=result["confirmed_products"];notes.append(result["message"])
  msg="; ".join(notes); c=conn();c.execute("UPDATE poketrack_scan_runs SET finished_at=?,status='completed',found=?,message=? WHERE id=?",(now(),total,msg,rid));c.commit();c.close()
  return {"status":"completed","found":total,"message":msg}
 except Exception as e:
  log.exception("scan failed");c=conn();c.execute("UPDATE poketrack_scan_runs SET finished_at=?,status='error',found=?,message=? WHERE id=?",(now(),total,str(e)[:500],rid));c.commit();c.close()
  return {"status":"error","found":total,"message":str(e)},500
 finally:
  SCAN_LOCK.release()
HTML="""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PokeTrack Radar</title><style>
:root{color-scheme:dark;--bg:#090e19;--panel:#121b2b;--line:#26364d;--muted:#9aabc2;--accent:#62d6c5;--gold:#f5c76b}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#edf4ff;font:15px system-ui}header{padding:24px max(18px,calc((100% - 1240px)/2));background:#101a2a;border-bottom:1px solid var(--line)}h1{margin:0;font-size:26px}header p,.muted{color:var(--muted)}main{max-width:1240px;margin:auto;padding:20px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.panel,.stat{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}.stat b{font-size:25px;display:block}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}.toolbar input,.toolbar select,button{background:#1b2940;color:#fff;border:1px solid #3b506c;border-radius:9px;padding:11px}button{cursor:pointer;background:#176e68;border:0;font-weight:700}.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.tabs button{background:#24344d}.tabs button.active{background:#176e68}.item{padding:12px 0;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:12px}.item a{color:var(--accent)}.pill{border:1px solid #465b74;border-radius:20px;padding:3px 8px;font-size:12px;white-space:nowrap}.ok{color:#8ee0a9}.warn{color:var(--gold)}@media(max-width:760px){.grid{grid-template-columns:repeat(2,1fr)}.item{display:block}.item .pill{display:inline-block;margin-top:6px}}
</style></head><body><header><h1>⚡ PokeTrack Radar</h1><p>Physical store discovery · Pokémon product listings · Winchester, VA 22601 · 80-mile radius</p></header><main>
<div class="grid"><div class="stat"><b id="stores">—</b><span class="muted">Physical stores found</span></div><div class="stat"><b id="products">—</b><span class="muted">Product listings</span></div><div class="stat"><b id="stock">—</b><span class="muted">Branch-level products in stock</span></div><div class="stat"><b id="last">—</b><span class="muted">Last completed scan</span></div></div>
<section class="panel"><h2>Radar controls</h2><p class="muted">Store locations come from a researched retailer branch directory. Product pages are online listings; stock is only marked when a retailer page exposes explicit availability. No shelf-level inventory or quantities are assumed.</p><div class="toolbar"><button onclick="scan('all')">Run full scan</button><button onclick="scan('stores')">Refresh store directory</button><button onclick="scan('products')">Scan product listings</button><button onclick="scan('inventory')">Check store inventory</button><span id="msg" class="muted">Ready</span></div></section>
<div class="tabs"><button class="active" onclick="tab('stores',this)">Physical stores</button><button onclick="tab('products',this)">Product listings</button><button onclick="tab('history',this)">Scan history</button></div>
<section class="panel"><div class="toolbar"><input id="q" placeholder="Filter by store, city, product…" oninput="draw()"><select id="retailer" onchange="draw()"><option value="">All retailers</option></select><select id="availability" onchange="draw()"><option value="">All availability</option><option value="in_stock">Explicitly in stock</option><option value="out_of_stock">Explicitly sold out</option><option value="unknown">Unknown</option></select></div><div id="list" class="muted">Loading…</div></section>
<p class="muted">Store locations are loaded from the researched branch directory. No map service is used. Product links open retailer websites. Availability can change and must be confirmed with the retailer.</p></main><script>
let mode='stores',stores=[],products=[],history=[],inventory=[];function tab(m,b){mode=m;document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('active'));b.classList.add('active');draw()}
async function load(){try{let [a,b,c,d,e]=await Promise.all(['/api/stores','/api/products','/api/history','/api/summary','/api/inventory'].map(x=>fetch(x).then(r=>r.json())));stores=a.stores||[];products=b.products||[];history=c.runs||[];inventory=e.inventory||[];document.getElementById('stores').textContent=stores.length;document.getElementById('products').textContent=products.length;document.getElementById('stock').textContent=inventory.filter(x=>x.status==='in_stock').length;document.getElementById('last').textContent=d.last_scan?new Date(d.last_scan).toLocaleString():'—';let opts=[...new Set([...stores,...products].map(x=>x.retailer))].sort();document.getElementById('retailer').innerHTML='<option value="">All retailers</option>'+opts.map(x=>'<option>'+esc(x)+'</option>').join('');draw()}catch(e){document.getElementById('list').textContent='Could not load dashboard data: '+e}}
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function draw(){let q=document.getElementById('q').value.toLowerCase(),r=document.getElementById('retailer').value,a=document.getElementById('availability').value;let rows=mode==='stores'?stores:mode==='products'?products:history;rows=rows.filter(x=>(!r||x.retailer===r)&&(!a||x.availability===a)&&JSON.stringify(x).toLowerCase().includes(q));document.getElementById('list').innerHTML=rows.length?rows.map(x=>mode==='stores'?'<div class="item"><div><b>'+esc(x.name)+'</b><div class="muted">'+esc(x.retailer)+' · '+esc(x.address||x.city||'Address not listed')+'</div><div class="muted">'+Number(x.distance).toFixed(1)+' miles from 22601 · <a href="'+esc(x.source_url)+'" target="_blank">Store page</a></div><div class="ok"><b>'+(x.inventory_check_status==='checked'&&Number(x.in_stock_products||0)>0?Number(x.in_stock_products)+' Pokémon products reported in stock':x.inventory_check_status==='no_match'?'Checked — no branch-specific stock result found':x.inventory_check_status==='error'?'Inventory check failed — stock unknown':x.inventory_check_status==='not_supported'?'Not checked — no matching product pages':x.inventory_check_status==='checked'?'Checked — no products reported in stock':'Local inventory not checked')+'</b></div><details><summary>View branch inventory</summary>'+((inventory.filter(y=>y.store_id===x.id).length)?inventory.filter(y=>y.store_id===x.id).map(y=>'<div class="muted">'+esc(y.product_title)+' — '+(y.status==='in_stock'?'In stock — '+esc(y.quantity_label):y.status==='out_of_stock'?'Out of stock':'Unknown')+' · '+esc(y.checked_at||'')+' · <a href="'+esc(y.source_url)+'" target="_blank">Product page</a></div>').join(''):'<div class="muted">No verified branch-specific product results. This does not mean the store is out of stock.</div>')+'</details></div><span class="pill">'+esc(x.inventory_check_time?'Checked '+new Date(x.inventory_check_time).toLocaleString():'Not checked')+'</span></div>':mode==='products'?'<div class="item"><div><b><a href="'+esc(x.url)+'" target="_blank">'+esc(x.title)+'</a></b><div class="muted">'+esc(x.retailer)+' · '+esc(x.price||'Price not listed')+'</div></div><span class="pill">'+esc(x.availability)+'</span></div>':'<div class="item"><div><b>'+esc(x.kind)+' · '+esc(x.status)+'</b><div class="muted">'+esc(x.message)+'</div></div><span class="pill">'+esc(x.started_at)+'</span></div>').join(''):'<p class="muted">No matching records yet. Run a scan to populate this section.</p>'}
async function scan(kind){let m=document.getElementById('msg');m.textContent='Starting scan…';try{let r=await fetch('/api/scan?kind='+kind,{method:'POST'}),d=await r.json();m.textContent=d.message||d.error||'Scan started';if(!r.ok)return;let tries=0;let timer=setInterval(async()=>{tries++;await load();let running=history.some(x=>x.status==='running');if(!running||tries>=180){clearInterval(timer);m.textContent=running?'Scan is still running; you can keep using the dashboard.':'Scan finished. '+(history[0]?.message||'Results refreshed.')}},4000)}catch(e){m.textContent='Scan failed: '+e}}
load();setInterval(load,60000);
</script></body></html>"""
@app.get("/")
def index(): return render_template_string(HTML)
@app.get("/health")
def health():
 try: c=conn();c.execute("SELECT 1");c.close();return jsonify(ok=True,service="PokeTrack")
 except Exception as e:return jsonify(ok=False,error=str(e)),500
@app.get("/api/stores")
def api_stores():
 c=conn();rows=[dict(x) for x in c.execute("""SELECT s.*,COALESCE(SUM(CASE WHEN i.status='in_stock' THEN 1 ELSE 0 END),0) AS in_stock_products,MAX(i.checked_at) AS inventory_checked_at,MAX(k.status) AS inventory_check_status,MAX(k.checked_at) AS inventory_check_time FROM stores s LEFT JOIN store_inventory i ON i.store_id=s.id LEFT JOIN store_inventory_checks k ON k.store_id=s.id GROUP BY s.id ORDER BY s.distance,s.retailer""")];c.close();return jsonify(stores=rows)
@app.get("/api/inventory")
def api_inventory():
 c=conn();rows=[dict(x) for x in c.execute("""SELECT i.*,s.name AS store_name,s.retailer,s.address,s.city,s.state FROM store_inventory i JOIN stores s ON s.id=i.store_id ORDER BY s.distance,s.retailer,s.name,i.product_title""")];c.close();return jsonify(inventory=rows)
@app.get("/api/products")
def api_products():
 c=conn();rows=[dict(x) for x in c.execute("SELECT * FROM products ORDER BY last_seen DESC LIMIT 2000")];c.close();return jsonify(products=rows)
@app.get("/api/history")
def api_history():
 c=conn();rows=[dict(x) for x in c.execute("SELECT * FROM poketrack_scan_runs ORDER BY id DESC LIMIT 100")];c.close();return jsonify(runs=rows)
@app.get("/api/summary")
def summary():
 c=conn();last=c.execute("SELECT finished_at FROM poketrack_scan_runs WHERE status='completed' ORDER BY id DESC LIMIT 1").fetchone();c.close();return jsonify(last_scan=last[0] if last else None,zip=ZIP,radius=RADIUS)
@app.post("/api/scan")
def api_scan():
 kind=request.args.get("kind","all")
 if kind not in ("all","stores","products","inventory"):return jsonify(error="Invalid scan kind"),400
 if not SCAN_LOCK.acquire(blocking=False):
  return jsonify({"status":"busy","message":"A scan is already running. Please wait for it to finish."}),409
 SCAN_LOCK.release()
 worker=threading.Thread(target=run_scan,args=(kind,),daemon=True)
 worker.start()
 return jsonify({"status":"started","message":"Scan started in the background. The dashboard will remain available while it runs."}),202
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.getenv("PORT","8080")))
