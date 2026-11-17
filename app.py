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
RADIUS=int(os.getenv("RADIUS_MILES","200"))
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
 lat,lon=zip_origin(ZIP)
 # Search by retailer identity tags, not only shop=*; many chains are mapped
 # with brand/operator/name tags and some features are ways or relations.
 pattern='(?i)target|walmart|best ?buy|costco|sam.?s club|walgreens|dollar general|barnes ?(&|and) ?noble|hobby lobby|books.?a.?million|gabe.?s'
 around=f'(around:{RADIUS*1609},{lat},{lon})'
 query='[out:json][timeout:60];('
 for tag in ('name','brand','operator'):
  query+=f'nwr{around}["{tag}"~"{pattern}"];'
 query+=');out center tags;'
 endpoints=['https://overpass-api.de/api/interpreter','https://overpass.kumi.systems/api/interpreter','https://overpass.private.coffee/api/interpreter']
 data=None; errors=[]
 for endpoint in endpoints:
  try:
   response=requests.post(endpoint,data=query,headers=HEADERS,timeout=75)
   response.raise_for_status(); data=response.json(); break
  except Exception as e:
   errors.append(f'{endpoint}: {str(e)[:100]}')
 if data is None: raise RuntimeError('All OpenStreetMap search endpoints failed: '+'; '.join(errors))
 elements=data.get('elements',[]); saved=0; c=conn()
 aliases={
  'target':'Target','walmart':'Walmart','best buy':'Best Buy','bestbuy':'Best Buy',
  'costco':'Costco',"sam's club": "Sam's Club",'sams club':"Sam's Club",
  'walgreens':'Walgreens','dollar general':'Dollar General',
  'barnes & noble':'Barnes & Noble','barnes and noble':'Barnes & Noble',
  'hobby lobby':'Hobby Lobby','books-a-million':'Books-A-Million',
  'books a million':'Books-A-Million',"gabe's": "Gabe's",'gabes':'Gabe\'s'
 }
 for el in elements:
  tags=el.get('tags',{})
  raw=' '.join([tags.get('name',''),tags.get('brand',''),tags.get('operator','')]).strip()
  key=raw.lower()
  retailer=next((v for k,v in aliases.items() if k in key),None)
  if not retailer: continue
  p=el.get('center',el); y=p.get('lat'); x=p.get('lon')
  if y is None or x is None: continue
  dist=haversine(lat,lon,float(y),float(x))
  if dist>RADIUS: continue
  street=' '.join(filter(None,[tags.get('addr:housenumber'),tags.get('addr:street')]))
  addr=', '.join(filter(None,[street,tags.get('addr:city'),tags.get('addr:state'),tags.get('addr:postcode')]))
  c.execute('''INSERT INTO stores(retailer,name,address,city,state,postcode,lat,lon,distance,source,source_url,checked_at)
   VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(retailer,name,lat,lon) DO UPDATE SET address=excluded.address,
   distance=excluded.distance,checked_at=excluded.checked_at''',
   (retailer,tags.get('name') or tags.get('brand') or raw or retailer,addr,tags.get('addr:city'),tags.get('addr:state'),tags.get('addr:postcode'),y,x,round(dist,1),'OpenStreetMap','https://www.openstreetmap.org/'+el['type']+'/'+str(el['id']),now()))
  saved+=1
 c.commit(); c.close(); return saved
def clean_url(u):
 u=unquote(u or "")
 if u.startswith("/url?"): u=parse_qs(urlparse(u).query).get("q",[""])[0]
 p=urlparse(u)
 if p.scheme not in ("http","https") or not p.netloc:return None
 if p.hostname and ("google." in p.hostname or "bing.com" in p.hostname):return None
 return urlunparse((p.scheme,p.netloc,p.path,"","",""))
def search_products(retailer):
 domain=RETAILERS[retailer][0]
 q=f'site:{domain} Pokémon trading cards OR Pokémon TCG'
 found={}
 # Google can throttle shared cloud IPs; use Bing RSS as a fallback.
 try:
  r=requests.get("https://www.google.com/search",params={"q":q,"num":20},headers=HEADERS,timeout=20)
  if r.status_code == 429:
   raise requests.HTTPError("Google rate limited this server (HTTP 429)")
  r.raise_for_status()
  soup=BeautifulSoup(r.text,"html.parser")
  for a in soup.select("a[href]"):
   u=clean_url(a.get("href")); title=a.get_text(" ",strip=True)
   if u and (urlparse(u).hostname or "").endswith(domain) and title and re.search(r"pokemon|pokémon|trading card|tcg",title,re.I):
    found[u]=title[:250]
 except requests.RequestException as google_error:
  log.warning("Google unavailable for %s: %s; trying Bing RSS",retailer,google_error)
  r=requests.get("https://www.bing.com/search",params={"q":q,"format":"rss"},headers=HEADERS,timeout=20)
  r.raise_for_status()
  soup=BeautifulSoup(r.text,"html.parser")
  for item in soup.select("item"):
   u=clean_url(item.link.get_text(strip=True) if item.link else "")
   title=item.title.get_text(" ",strip=True) if item.title else ""
   if u and (urlparse(u).hostname or "").endswith(domain) and title:
    found[u]=title[:250]
 products=[]; c=conn()
 for u,title in list(found.items())[:10]:
  price=None; stock="unknown"; source="retailer search result"
  try:
   page=requests.get(u,headers=HEADERS,timeout=12)
   if page.ok:
    s=BeautifulSoup(page.text,"html.parser")
    og=s.find("meta",property="og:title")
    if og and og.get("content"): title=og["content"][:250]
    for sel in ['meta[property="product:price:amount"]','meta[itemprop="price"]']:
     n=s.select_one(sel)
     if n and n.get("content"): price=n["content"]; break
    body=s.get_text(" ",strip=True).lower()
    if re.search(r"out of stock|sold out|currently unavailable",body): stock="out_of_stock"
    elif re.search(r"add to cart|in stock|available now",body): stock="in_stock"
    source="retailer product page"
  except requests.RequestException: pass
  if not re.search(r"pokemon|pokémon|trading card|tcg",title,re.I): continue
  t=now(); c.execute("""INSERT INTO products(retailer,title,url,price,availability,source,first_seen,last_seen)
   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,price=COALESCE(excluded.price,products.price),
   availability=excluded.availability,source=excluded.source,last_seen=excluded.last_seen""",(retailer,title,u,price,stock,source,t,t))
  products.append({"retailer":retailer,"title":title,"url":u,"price":price,"availability":stock})
 c.commit(); c.close(); return products
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
  msg="; ".join(notes); c=conn();c.execute("UPDATE poketrack_scan_runs SET finished_at=?,status='completed',found=?,message=? WHERE id=?",(now(),total,msg,rid));c.commit();c.close()
  return {"status":"completed","found":total,"message":msg}
 except Exception as e:
  log.exception("scan failed");c=conn();c.execute("UPDATE poketrack_scan_runs SET finished_at=?,status='error',found=?,message=? WHERE id=?",(now(),total,str(e)[:500],rid));c.commit();c.close()
  return {"status":"error","found":total,"message":str(e)},500
 finally:
  SCAN_LOCK.release()
HTML="""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PokeTrack Radar</title><style>
:root{color-scheme:dark;--bg:#090e19;--panel:#121b2b;--line:#26364d;--muted:#9aabc2;--accent:#62d6c5;--gold:#f5c76b}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#edf4ff;font:15px system-ui}header{padding:24px max(18px,calc((100% - 1240px)/2));background:#101a2a;border-bottom:1px solid var(--line)}h1{margin:0;font-size:26px}header p,.muted{color:var(--muted)}main{max-width:1240px;margin:auto;padding:20px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.panel,.stat{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}.stat b{font-size:25px;display:block}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}.toolbar input,.toolbar select,button{background:#1b2940;color:#fff;border:1px solid #3b506c;border-radius:9px;padding:11px}button{cursor:pointer;background:#176e68;border:0;font-weight:700}.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.tabs button{background:#24344d}.tabs button.active{background:#176e68}.item{padding:12px 0;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:12px}.item a{color:var(--accent)}.pill{border:1px solid #465b74;border-radius:20px;padding:3px 8px;font-size:12px;white-space:nowrap}.ok{color:#8ee0a9}.warn{color:var(--gold)}@media(max-width:760px){.grid{grid-template-columns:repeat(2,1fr)}.item{display:block}.item .pill{display:inline-block;margin-top:6px}}
</style></head><body><header><h1>⚡ PokeTrack Radar</h1><p>Physical store discovery · Pokémon product listings · Winchester, VA 22601 · 200-mile radius</p></header><main>
<div class="grid"><div class="stat"><b id="stores">—</b><span class="muted">Physical stores found</span></div><div class="stat"><b id="products">—</b><span class="muted">Product listings</span></div><div class="stat"><b id="stock">—</b><span class="muted">Pages explicitly in stock</span></div><div class="stat"><b id="last">—</b><span class="muted">Last completed scan</span></div></div>
<section class="panel"><h2>Radar controls</h2><p class="muted">Store locations come from OpenStreetMap. Product pages are online listings; stock is only marked when a retailer page exposes explicit availability. No shelf-level inventory or quantities are assumed.</p><div class="toolbar"><button onclick="scan('all')">Run full scan</button><button onclick="scan('stores')">Refresh store directory</button><button onclick="scan('products')">Scan product listings</button><span id="msg" class="muted">Ready</span></div></section>
<div class="tabs"><button class="active" onclick="tab('stores',this)">Physical stores</button><button onclick="tab('products',this)">Product listings</button><button onclick="tab('history',this)">Scan history</button></div>
<section class="panel"><div class="toolbar"><input id="q" placeholder="Filter by store, city, product…" oninput="draw()"><select id="retailer" onchange="draw()"><option value="">All retailers</option></select><select id="availability" onchange="draw()"><option value="">All availability</option><option value="in_stock">Explicitly in stock</option><option value="out_of_stock">Explicitly sold out</option><option value="unknown">Unknown</option></select></div><div id="list" class="muted">Loading…</div></section>
<p class="muted">Store directory data © OpenStreetMap contributors. Product links open retailer websites. Availability can change and must be confirmed with the retailer.</p></main><script>
let mode='stores',stores=[],products=[],history=[];function tab(m,b){mode=m;document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('active'));b.classList.add('active');draw()}
async function load(){try{let [a,b,c,d]=await Promise.all(['/api/stores','/api/products','/api/history','/api/summary'].map(x=>fetch(x).then(r=>r.json())));stores=a.stores||[];products=b.products||[];history=c.runs||[];document.getElementById('stores').textContent=stores.length;document.getElementById('products').textContent=products.length;document.getElementById('stock').textContent=products.filter(x=>x.availability==='in_stock').length;document.getElementById('last').textContent=d.last_scan?new Date(d.last_scan).toLocaleString():'—';let opts=[...new Set([...stores,...products].map(x=>x.retailer))].sort();document.getElementById('retailer').innerHTML='<option value="">All retailers</option>'+opts.map(x=>'<option>'+esc(x)+'</option>').join('');draw()}catch(e){document.getElementById('list').textContent='Could not load dashboard data: '+e}}
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function draw(){let q=document.getElementById('q').value.toLowerCase(),r=document.getElementById('retailer').value,a=document.getElementById('availability').value;let rows=mode==='stores'?stores:mode==='products'?products:history;rows=rows.filter(x=>(!r||x.retailer===r)&&(!a||x.availability===a)&&JSON.stringify(x).toLowerCase().includes(q));document.getElementById('list').innerHTML=rows.length?rows.map(x=>mode==='stores'?'<div class="item"><div><b>'+esc(x.name)+'</b><div class="muted">'+esc(x.retailer)+' · '+esc(x.address||x.city||'Address not listed')+'</div><div class="muted">'+Number(x.distance).toFixed(1)+' miles from '+esc(x.zip||'22601')+' · <a href="'+esc(x.source_url)+'" target="_blank">Map/source</a></div></div><span class="pill">'+esc(x.source)+'</span></div>':mode==='products'?'<div class="item"><div><b><a href="'+esc(x.url)+'" target="_blank">'+esc(x.title)+'</a></b><div class="muted">'+esc(x.retailer)+' · '+esc(x.price||'Price not listed')+'</div></div><span class="pill">'+esc(x.availability)+'</span></div>':'<div class="item"><div><b>'+esc(x.kind)+' · '+esc(x.status)+'</b><div class="muted">'+esc(x.message)+'</div></div><span class="pill">'+esc(x.started_at)+'</span></div>').join(''):'<p class="muted">No matching records yet. Run a scan to populate this section.</p>'}
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
 c=conn();rows=[dict(x) for x in c.execute("SELECT * FROM stores ORDER BY distance,retailer")];c.close();return jsonify(stores=rows)
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
 if kind not in ("all","stores","products"):return jsonify(error="Invalid scan kind"),400
 if not SCAN_LOCK.acquire(blocking=False):
  return jsonify({"status":"busy","message":"A scan is already running. Please wait for it to finish."}),409
 SCAN_LOCK.release()
 worker=threading.Thread(target=run_scan,args=(kind,),daemon=True)
 worker.start()
 return jsonify({"status":"started","message":"Scan started in the background. The dashboard will remain available while it runs."}),202
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.getenv("PORT","8080")))
