import os, re, json, math, time, sqlite3, logging, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urlunparse, parse_qs, unquote
import requests
try:
 from curl_cffi import requests as browser_requests
except Exception:
 browser_requests=None
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, render_template_string

app=Flask(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log=logging.getLogger("poketrack")
SCAN_LOCK=threading.Lock()
DB_INIT_LOCK=threading.Lock()
DB_READY=False
GOOGLE_RATE_LIMITED=False
DB_PATH=os.getenv("DB_PATH","/data/poketrack.db")
ZIP=os.getenv("START_ZIP","22601")
RADIUS=int(os.getenv("RADIUS_MILES","80"))
UA=os.getenv("USER_AGENT","Mozilla/5.0 (compatible; PokeTrackRadar/1.0)")
HEADERS={"User-Agent":UA,"Accept-Language":"en-US,en;q=0.9"}
JINA_CALLS=0
JINA_CALL_LIMIT=48
JINA_LOCK=threading.Lock()
def jina_read_url(target_url, timeout=8):
 global JINA_CALLS
 with JINA_LOCK:
  if JINA_CALLS>=JINA_CALL_LIMIT: return None
  JINA_CALLS+=1
 try:
  reader_url="https://r.jina.ai/"+target_url
  response=requests.get(reader_url,headers={"Accept":"text/plain","User-Agent":"Mozilla/5.0"},timeout=timeout)
  if not response.ok:
   log.warning("Jina Reader returned HTTP %s for %s",response.status_code,target_url[:160])
   return None
  log.info("Jina Reader fetched %s (%s chars)",target_url[:120],len(response.text))
  return response
 except requests.RequestException as e:
  log.warning("Jina Reader failed for %s: %s",target_url[:160],e)
  return None

def search_get(url, **kwargs):
 if browser_requests is not None:
  headers={k:v for k,v in {**HEADERS,**kwargs.pop("headers",{})}.items() if k.lower()!="user-agent"}
  headers.setdefault("Referer","https://www.google.com/")
  return browser_requests.get(url,headers=headers,impersonate="chrome151",**kwargs)
 return requests.get(url,**kwargs)

def retailer_get(url, **kwargs):
 host=(urlparse(url).hostname or "").lower()
 protected=("target.com","walmart.com","barnesandnoble.com","bestbuy.com","samsclub.com","costco.com")
 if browser_requests is not None and any(host==d or host.endswith("."+d) for d in protected):
  headers={k:v for k,v in {**HEADERS,**kwargs.pop("headers",{})}.items() if k.lower()!="user-agent"}
  headers.setdefault("Referer","https://www.google.com/")
  try:
   return browser_requests.get(url,headers=headers,impersonate="chrome151",**kwargs)
  except Exception as e:
   log.warning("Browser-impersonated request failed for %s; retrying standard HTTP: %s",host,e)
 return requests.get(url,**kwargs)

BROWSER_LOCK=threading.RLock()
BROWSER_MANAGER=None
BROWSER_INSTANCE=None
BROWSER_CONTEXT=None
BROWSER_PAGE_COUNT=0

def _close_browser():
 global BROWSER_MANAGER,BROWSER_INSTANCE,BROWSER_CONTEXT,BROWSER_PAGE_COUNT
 try:
  if BROWSER_CONTEXT is not None: BROWSER_CONTEXT.close()
 except Exception: pass
 try:
  if BROWSER_INSTANCE is not None: BROWSER_INSTANCE.close()
 except Exception: pass
 try:
  if BROWSER_MANAGER is not None: BROWSER_MANAGER.stop()
 except Exception: pass
 BROWSER_MANAGER=None; BROWSER_INSTANCE=None; BROWSER_CONTEXT=None; BROWSER_PAGE_COUNT=0

def _start_browser():
 global BROWSER_MANAGER,BROWSER_INSTANCE,BROWSER_CONTEXT
 from playwright.sync_api import sync_playwright
 BROWSER_MANAGER=sync_playwright().start()
 BROWSER_INSTANCE=BROWSER_MANAGER.chromium.launch(headless=True,args=[
  "--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
  "--disable-background-networking","--disable-extensions",
  "--disable-features=Translate,BackForwardCache"
 ])
 BROWSER_CONTEXT=BROWSER_INSTANCE.new_context(
  locale="en-US",timezone_id="America/New_York",
  viewport={"width":1280,"height":800},device_scale_factor=1,
  java_script_enabled=True)
 log.info("Lightweight Chromium session started")

def browser_get(url, timeout=25, store=None):
 # One lightweight page at a time. Drop expensive assets and recycle the
 # browser periodically so retailer pages cannot accumulate renderer memory.
 global BROWSER_MANAGER,BROWSER_INSTANCE,BROWSER_CONTEXT,BROWSER_PAGE_COUNT
 from types import SimpleNamespace
 with BROWSER_LOCK:
  if BROWSER_INSTANCE is not None:
   try:
    if not BROWSER_INSTANCE.is_connected(): _close_browser()
   except Exception: _close_browser()
  if BROWSER_INSTANCE is None:
   try: _start_browser()
   except Exception:
    _close_browser()
    raise
  if BROWSER_PAGE_COUNT>=8:
   log.info("Recycling Chromium after %s product pages",BROWSER_PAGE_COUNT)
   _close_browser(); _start_browser()
  page=None
  try:
   page=BROWSER_CONTEXT.new_page()
   page.set_default_timeout(min(8000,timeout*1000))
   page.route("**/*",lambda route: route.abort() if route.request.resource_type in ("image","font","media") else route.continue_())
   response=page.goto(url,wait_until="domcontentloaded",timeout=timeout*1000)
   if store and "target.com" in (urlparse(url).hostname or ""):
    try:
     for label in ("Change store","Choose store","Edit store","My store"):
      locator=page.get_by_text(label,exact=False).first
      if locator.count():
       locator.click(timeout=1200); break
     fields=page.locator("input:visible")
     for idx in range(min(fields.count(),6)):
      field=fields.nth(idx)
      hint=((field.get_attribute("placeholder") or "")+" "+(field.get_attribute("aria-label") or "")).lower()
      if any(x in hint for x in ("zip","city","state","location","store")):
       field.fill(str(store.get("postcode") or store.get("city") or ""))
       page.wait_for_timeout(500)
       target_label=store.get("name") or store.get("city") or ""
       option=page.get_by_text(target_label,exact=False).last
       if option.count(): option.click(timeout=1200)
       for action in ("Save","Use this store","Select store","Set as my store","Confirm"):
        btn=page.get_by_role("button",name=re.compile(action,re.I)).first
        if btn.count():
         btn.click(timeout=1200); break
       break
    except Exception as e:
     log.info("Visible store selection not completed for %s: %s",store.get("name"),str(e)[:120])
   html=page.content()
   status=response.status if response else 200
   final_url=page.url
   BROWSER_PAGE_COUNT+=1
   return SimpleNamespace(ok=status<400,status_code=status,text=html,url=final_url,
    raise_for_status=lambda: (_ for _ in ()).throw(requests.HTTPError(f"HTTP {status} for {final_url}")) if status>=400 else None)
  except Exception as e:
   log.warning("Chromium page failed for %s: %s; restarting browser",url[:160],str(e)[:180])
   _close_browser()
   raise
  finally:
   if page is not None:
    try: page.close()
    except Exception: pass

RETAILERS={
 "Target":["target.com"],"Walmart":["walmart.com"],"Best Buy":["bestbuy.com"],
"Barnes & Noble":["barnesandnoble.com"],"Costco":["costco.com"],
 "Sam's Club":["samsclub.com"],"Walgreens":["walgreens.com"],"Dollar General":["dollargeneral.com"],
 "Hobby Lobby":["hobbylobby.com"],"Books-A-Million":["booksamillion.com"],"Gabe's":["gabesstores.com"]
}
SCHEMA="""
CREATE TABLE IF NOT EXISTS stores(id INTEGER PRIMARY KEY, retailer TEXT NOT NULL, name TEXT NOT NULL, address TEXT,
 city TEXT, state TEXT, postcode TEXT, lat REAL NOT NULL, lon REAL NOT NULL, distance REAL NOT NULL,
 source TEXT NOT NULL, source_url TEXT, checked_at TEXT NOT NULL, UNIQUE(retailer,name,lat,lon));
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, retailer TEXT NOT NULL, title TEXT NOT NULL,
 url TEXT NOT NULL UNIQUE, price TEXT, availability TEXT NOT NULL DEFAULT 'unknown',
 source TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,last_checked TEXT NOT NULL DEFAULT '',
 seller_verified INTEGER NOT NULL DEFAULT 0, seller_name TEXT, popularity_score INTEGER NOT NULL DEFAULT 0, popularity_reason TEXT);
CREATE TABLE IF NOT EXISTS poketrack_scan_runs(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, status TEXT NOT NULL, found INTEGER DEFAULT 0, message TEXT,
 progress INTEGER NOT NULL DEFAULT 0, phase TEXT NOT NULL DEFAULT '', current INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS store_inventory(id INTEGER PRIMARY KEY,store_id INTEGER NOT NULL,product_url TEXT NOT NULL,product_title TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'unknown',quantity INTEGER,quantity_label TEXT NOT NULL DEFAULT 'count unavailable',source_url TEXT,detail TEXT,checked_at TEXT NOT NULL,UNIQUE(store_id,product_url));
CREATE TABLE IF NOT EXISTS store_inventory_checks(store_id INTEGER PRIMARY KEY,status TEXT NOT NULL,checked_at TEXT NOT NULL,detail TEXT);
CREATE TABLE IF NOT EXISTS retailer_scan_status(retailer TEXT PRIMARY KEY,status TEXT NOT NULL,checked_at TEXT NOT NULL,product_count INTEGER NOT NULL DEFAULT 0,detail TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS product_stock_history(id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL,checked_at TEXT NOT NULL,status TEXT NOT NULL,price TEXT,source TEXT NOT NULL DEFAULT 'retailer product page',evidence TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_product_stock_history_product_time ON product_stock_history(product_id,checked_at DESC);
CREATE TABLE IF NOT EXISTS restock_events(id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL,retailer TEXT NOT NULL,title TEXT NOT NULL,url TEXT NOT NULL,detected_at TEXT NOT NULL,previous_status TEXT NOT NULL,current_status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS product_requests(id INTEGER PRIMARY KEY,product_title TEXT NOT NULL,retailer TEXT,product_url TEXT,notes TEXT,status TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL);
"""
def now(): return datetime.now(timezone.utc).isoformat()
def conn():
 global DB_READY
 os.makedirs(os.path.dirname(DB_PATH) or ".",exist_ok=True)
 c=sqlite3.connect(DB_PATH,timeout=60,check_same_thread=True)
 c.row_factory=sqlite3.Row
 c.execute("PRAGMA busy_timeout=60000")
 # Initialize persistent schema once; repeating DDL on every request caused
 # avoidable write contention while dashboard reads and scan writes overlapped.
 if not DB_READY:
  with DB_INIT_LOCK:
   if not DB_READY:
    mode=c.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(mode).lower()!="wal":
     raise RuntimeError("SQLite WAL mode could not be enabled; got "+str(mode))
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript(SCHEMA)
    scan_cols={r[1] for r in c.execute("PRAGMA table_info(poketrack_scan_runs)")}
    for name,decl in [("progress","INTEGER NOT NULL DEFAULT 0"),("phase","TEXT NOT NULL DEFAULT ''"),("current","INTEGER NOT NULL DEFAULT 0"),("total","INTEGER NOT NULL DEFAULT 0")]:
     if name not in scan_cols: c.execute(f"ALTER TABLE poketrack_scan_runs ADD COLUMN {name} {decl}")
    cols={r[1] for r in c.execute("PRAGMA table_info(products)")}
    for name,decl in [("last_checked","TEXT NOT NULL DEFAULT ''"),("seller_verified","INTEGER NOT NULL DEFAULT 0"),("seller_name","TEXT"),("popularity_score","INTEGER NOT NULL DEFAULT 0"),("popularity_reason","TEXT"),("discovery_sources","TEXT"),("stock_checked_at","TEXT"),("monitor_enabled","INTEGER NOT NULL DEFAULT 1"),("check_interval_seconds","INTEGER NOT NULL DEFAULT 1800"),("next_check_at","TEXT"),("last_status_change","TEXT")]:
     if name not in cols: c.execute(f"ALTER TABLE products ADD COLUMN {name} {decl}")
    # A worker thread cannot survive a Railway container restart. Close stale
    # running rows so the dashboard never displays a permanently frozen scan.
    c.execute("UPDATE poketrack_scan_runs SET status='interrupted',finished_at=?,phase='Interrupted by service restart' WHERE status='running'",(now(),))
    stamp=now()
    c.execute("UPDATE products SET check_interval_seconds=CASE WHEN popularity_score>=25 THEN 300 WHEN popularity_score>=15 THEN 600 ELSE 1800 END,next_check_at=CASE WHEN next_check_at IS NULL OR next_check_at='' THEN ? ELSE next_check_at END WHERE seller_verified=1",(stamp,))
    c.commit()
    DB_READY=True
 else:
  c.execute("PRAGMA synchronous=NORMAL")
 return c

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
 if "duckduckgo.com/l/" in u: u=parse_qs(urlparse(u).query).get("uddg",[u])[0]
 p=urlparse(u)
 if p.hostname and ("google." in p.hostname or p.hostname.endswith(".google.com")):
  params=parse_qs(p.query)
  u=(params.get("q") or params.get("url") or params.get("adurl") or [""])[0]
  p=urlparse(u)
 if p.scheme not in ("http","https") or not p.netloc:return None
 if p.hostname and ("google." in p.hostname or "bing.com" in p.hostname):return None
 return urlunparse((p.scheme,p.netloc,p.path,"","",""))

def direct_catalog_url(retailer):
 # Public retailer catalog/search pages; no search-engine result pages or paid APIs.
 urls={
  "Target":"https://www.target.com/c/trading-card-games-cards-toys/pokemon/-/N-d4gjqZ569t0",
  "Walmart":"https://www.walmart.com/cp/pokemon/3580439",
  "Walgreens":"https://www.walgreens.com/search/results.jsp?Ntt=pokemon%20trading%20cards",
  "Best Buy":"https://www.bestbuy.com/site/pokemon/pokemon-trading-cards/pcmcat1741200145801.c",
  "Barnes & Noble":"https://www.barnesandnoble.com/collections/toys-games/games/collectible-card-games-1",
  "Costco":"https://www.costco.com/CatalogSearch?keyword=pokemon",
  "Sam's Club":"https://www.samsclub.com/browse/Pokemon/16860219",
  "Dollar General":"https://www.dollargeneral.com/c/toys?page=4",
  "Hobby Lobby":"https://www.hobbylobby.com/search/?q=pokemon",
  "Books-A-Million":"https://www.booksamillion.com/search?query=pokemon+trading+cards",
  "Gabe's":"https://www.gabesstores.com/",
  "Amazon":"https://www.amazon.com/s?k=pokemon+trading+cards"
 }
 return urls.get(retailer)
def is_pokemon_tcg_title(title):
 # Exclude licensed books and unrelated Pokémon merchandise.
 title=re.sub(r"\s+"," ",(title or "").lower()).strip()
 if not re.search(r'pokemon|pokémon',title): return False
 if re.search(r'\b(book|paperback|hardcover|story|stories|novel|guide|calendar|advent calendar|figure|plush|costume|apparel|backpack|toy car|lunch box|video game|nintendo switch)\b',title): return False
 return bool(re.search(r'trading card|\btc[g]?\b|booster|elite trainer|\betb\b|blister|tin|deck|card pack|card game|pokemon cards|pokémon cards',title))
def verify_retailer_seller(retailer, page_text):
 text=(page_text or "").lower()
 aliases={"Target":("target",),"Walmart":("walmart",),"Best Buy":("best buy","bestbuy")}
 sellers=re.findall(r'"seller"\s*:\s*\{[^{}]*?"name"\s*:\s*"([^"]+)"',text,re.I)
 sellers+=re.findall(r'sold\s*(?:and|&)\s*shipped\s*by\s+([a-z0-9 .&-]{2,60})',text,re.I)
 sellers+=re.findall(r'sold by\s+([a-z0-9 .&-]{2,60})',text,re.I)
 sellers=[re.sub(r"\s+"," ",x).strip(" .,:;") for x in sellers]
 if sellers:
  expected=aliases.get(retailer,(retailer.lower(),))
  if any(any(alias in name.lower() for alias in expected) for name in sellers):
   return True,retailer
  return False,None
 if re.search(r"more options from marketplace sellers|marketplace only|third.party seller|sold by a third party|fulfilled by a third party",text):
  return False,None
 # Do not silently treat an unnamed offer as first-party on marketplaces.
 # Walmart, Target, Best Buy, and Amazon must expose an explicit retailer seller.
 if retailer in ("Walmart","Target","Best Buy","Amazon"):
  return False,None
 return True,"retailer-owned domain (no marketplace seller indicated)"

def popularity_for(title, page_text):
 t=(title or "").lower(); body=(page_text or "").lower(); score=0; reason=[]
 for pts,term in [(35,"30th celebration"),(32,"ascended heroes"),(30,"chaos rising"),(28,"pitch black"),(27,"perfect order"),(26,"prismatic evolutions"),(25,"phantasmal flames"),(24,"151"),(23,"destined rivals"),(22,"pokemon day 2026"),(20,"first partner illustration"),(19,"delta reign"),(18,"black bolt"),(18,"white flare"),(17,"journey together"),(16,"surging sparks"),(15,"mega evolution")]:
  if term in t: score=max(score,pts); reason.append(term.title())
 if re.search(r"best.?sell|popular|top seller|customer favorite",body): score+=8; reason.append("retailer popularity")
 return min(score,100),(", ".join(reason) or "standard TCG listing")
def retailer_domain_match(url,domain):
 host=(urlparse(url).hostname or "").lower()
 domain=(domain or "").lower()
 return host==domain or host.endswith("."+domain)
def online_stock_status(page_text, embedded_text="", retailer=None):
 text=((page_text or "")+" "+(embedded_text or "")).lower()
 if re.search(r"captcha|robot or human|access denied|verify you are human|unusual traffic",text[:30000]): return "unknown"
 if re.search(r'aria-disabled="true"[^>]*>.*?add to cart|<button[^>]*disabled[^>]*>.*?add to cart|add to cart.{0,200}(?:disabled|aria-disabled="true")',text,re.S): return "unknown"
 if re.search(r'"availability"\s*:\s*"[^"]*(?:OutOfStock|SoldOut)',text): return "out_of_stock"
 if re.search(r'"availability"\s*:\s*"[^"]*(?:InStock|LimitedAvailability)',text):
  if retailer=="Target" and not re.search(r"add to cart|pickup today|available for pickup|available for shipping|ship it",text):
   return "unknown"
  return "in_stock"
 if re.search(r'"availability_status"\s*:\s*"out_of_stock"|"availabilityStatus"\s*:\s*"out_of_stock"',text): return "out_of_stock"
 if re.search(r'"availability_status"\s*:\s*"in_stock"|"availabilityStatus"\s*:\s*"in_stock"',text): return "in_stock"
 if re.search(r"out of stock|sold out|currently unavailable|not available online",text): return "out_of_stock"
 if re.search(r"add to cart|available to ship|in stock online|available online|ready to ship|available for pickup",text):
  if retailer=="Target" and re.search(r"loading|placeholder|disabled",text): return "unknown"
  return "in_stock"
 return "unknown"

SEED_PRODUCT_URLS={
 "Target":["https://www.target.com/p/-/A-1010892076","https://www.target.com/p/-/A-1011483406"],
 "Walmart":["https://www.walmart.com/ip/15718673510","https://www.walmart.com/ip/20161351456","https://www.walmart.com/ip/20754418655","https://business.walmart.com/ip/POKEMON-SV10-DESTINED-RIVALS-D7-ELITE-TRAINER-BOX/19965460207"],
 "Dollar General":["https://www.dollargeneral.com/p/pok%25c3%25a9mon-trading-card-pack%2C-10-ct/728192507076"],
 "Sam's Club":[
  "https://www.samsclub.com/ip/pokemon-4-mini-tins-4-promo-cards-bundle/17593918944",
  "https://www.samsclub.com/ip/pokemon-poke-ball-two-mini-tins-assorted-styles/13872200589",
  "https://www.samsclub.com/ip/seort/18430356539",
  "https://www.samsclub.com/ip/pokemon-paldea-legends-tins-2-pk/16169807769",
  "https://www.samsclub.com/ip/Pok-mon-Collector-Chest-and-Level-Poke-Ball/15337605373"
 ],
 "Best Buy":[
  "https://www.bestbuy.com/product/pokemon-trading-card-game-30th-celebration-elite-trainer-box/JJG2TL8XCJ",
  "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-pitch-black-elite-trainer-box/JJG2TL8J45",
  "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-chaos-rising-elite-trainer-box/JJG2TL34RT",
  "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-delta-reign-elite-trainer-box/JJG2TL8QGY",
  "https://www.bestbuy.com/product/pokemon-trading-card-game-mega-evolution-perfect-order-elite-trainer-box/JJG2TL3W86",
  "https://www.bestbuy.com/product/pokemon-trading-card-game-30th-celebration-ultra-premium-collection-day-or-night-1-ultra-premium-collection-per-order-styles-may-vary/JJG2TL8254"
 ]
}

def retailer_product_url_is_valid(retailer,url):
 path=(urlparse(url).path or "").lower()
 if retailer=="Target": return path.startswith("/p/") and bool(re.search(r"/a-\d+",path))
 if retailer=="Walmart": return bool(re.match(r"^/ip/(?:[^/]+/)?\d+",path))
 if retailer=="Best Buy": return path.startswith("/product/") or bool(re.match(r"^/site/.+\.p",path))
 if path.startswith(("/search","/s/","/q/","/c/","/category/","/browse/")): return False
 return True

def search_products(retailer, progress_callback=None, max_candidates=None):
 global GOOGLE_RATE_LIMITED
 catalog=direct_catalog_url(retailer)
 domain=RETAILERS[retailer][0]
 found={}
 def add_candidate(raw_url,title):
  u=clean_url(raw_url)
  title=(title or "").strip()
  if not u or not retailer_domain_match(u,domain) or not retailer_product_url_is_valid(retailer,u): return
  if not title or not is_pokemon_tcg_title(title): return
  found.setdefault(u,title[:250])
 for seed in SEED_PRODUCT_URLS.get(retailer,[]):
  seed_url=clean_url(seed)
  if seed_url and retailer_domain_match(seed_url,domain) and retailer_product_url_is_valid(retailer,seed_url):
   found.setdefault(seed_url,"Pokemon TCG Elite Trainer Box")
 if catalog:
  try:
   # Target category listings are paginated. Walk the visible category pages and
   # collect every retailer-owned product link exposed by the page, not just a
   # small hand-picked sample. Stop at the retailer's last page or a safe ceiling.
   page_urls=[catalog]
   seen_pages=set()
   while page_urls and len(seen_pages)<(30 if retailer=="Target" else 10):
    page_url=page_urls.pop(0)
    if page_url in seen_pages: continue
    seen_pages.add(page_url)
    if progress_callback: progress_callback(0.02+0.18*len(seen_pages)/30,f"{retailer}: reading catalog page {len(seen_pages)} (up to 30)")
    r=retailer_get(page_url,headers=HEADERS,timeout=18)
    r.raise_for_status()
    soup=BeautifulSoup(r.text,"html.parser")
    for a in soup.select("a[href]"):
     title=a.get_text(" ",strip=True)
     if not title:
      image_node=a.find("img")
      title=(image_node.get("alt") if image_node else None) or a.get("aria-label") or a.get("title") or ""
     if not title and a.parent: title=a.parent.get_text(" ",strip=True)
     add_candidate(requests.compat.urljoin(page_url,a.get("href","")),title)
    # Follow retailer-provided next-page links for every catalog, not just Target.
    for selector in ('a[rel="next"]','a[aria-label*="Next" i]','a[title*="Next" i]','a.next','.pagination a.next'):
      nxt=soup.select_one(selector)
      if nxt and nxt.get("href"):
       nu=requests.compat.urljoin(page_url,nxt["href"])
       if nu not in seen_pages: page_urls.append(nu)
       break
    if retailer=="Target" and not page_urls and len(seen_pages)<30:
     from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
     parts=urlsplit(page_url); params=dict(parse_qsl(parts.query))
     current=int(params.get("Nao","0") or 0)
     params["Nao"]=str(current+24)
     next_url=urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(params),parts.fragment))
     if next_url not in seen_pages: page_urls.append(next_url)
   def walk_catalog_json(obj):
    if isinstance(obj,dict):
     name=obj.get("name") or obj.get("title") or obj.get("productName")
     url=obj.get("url") or obj.get("productUrl") or obj.get("productURL") or obj.get("canonicalUrl") or obj.get("href")
     if isinstance(name,str) and isinstance(url,str):
      add_candidate(requests.compat.urljoin(catalog,url),name)
     for value in obj.values():
      if isinstance(value,(dict,list)): walk_catalog_json(value)
    elif isinstance(obj,list):
     for value in obj:
      if isinstance(value,(dict,list)): walk_catalog_json(value)
   for node in soup.select('script[type="application/ld+json"],script#__NEXT_DATA__,script[type="application/json"]'):
    try:
     data=json.loads(node.string or node.get_text())
     walk_catalog_json(data)
    except Exception: pass
  except Exception as e:
   log.warning("Direct catalog unavailable for %s: %s; using public search fallback",retailer,e)
 # Target's catalog is rendered dynamically and may return an incomplete page to
 # ordinary HTTP clients. Use Jina Reader's text rendering as a fallback to
 # collect actual Target product-page links across its paginated catalog.
 if retailer=="Target" and len(found)<20:
  from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
  for page_no in range(32):
   parts=urlsplit(catalog); params=dict(parse_qsl(parts.query))
   params["Nao"]=str(page_no*24)
   target_page=urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(params),parts.fragment))
   if progress_callback: progress_callback(0.08+0.10*(page_no+1)/32,f"Target: reading catalog results page {page_no+1}/32")
   rendered=jina_read_url(target_page,timeout=20)
   if not rendered or not rendered.ok:
    log.warning("Target rendered catalog page %s could not be read",page_no+1)
    continue
   page_text=rendered.text or ""
   page_added=0
   for match in re.finditer(r'\[([^\]\n]{3,250})\]\((https?://[^)\s]+)\)',page_text):
    title=re.sub(r"^\d+†","",match.group(1)).strip()
    url=match.group(2).rstrip(".,")
    before=len(found)
    add_candidate(url,title)
    if len(found)>before: page_added+=1
   log.info("Target rendered catalog page %s: %s product URLs collected (%s total)",page_no+1,page_added,len(found))
   if page_no>=3 and page_added==0:
    break
 # Retailer-owned XML sitemaps are a no-search-engine discovery path and
 # can expose product URLs even when the site's search page is JS-rendered.
 sitemap_queue=[]
 if progress_callback: progress_callback(0.22,f"{retailer}: checking retailer sitemap index")
 try:
  robots=retailer_get("https://www."+domain+"/robots.txt",headers=HEADERS,timeout=8)
  if robots.ok:
   for line in robots.text.splitlines():
    if line.lower().startswith("sitemap:"):
     sitemap_queue.append(line.split(":",1)[1].strip())
 except Exception as e:
  log.warning("Robots.txt unavailable for %s: %s",retailer,e)
 if not sitemap_queue: sitemap_queue=[f"https://www.{domain}/sitemap.xml"]
 sitemap_seen=set()
 sitemap_product_count=0
 while sitemap_queue and len(sitemap_seen)<40 and sitemap_product_count<500:
  sitemap_url=sitemap_queue.pop(0)
  if sitemap_url in sitemap_seen: continue
  sitemap_seen.add(sitemap_url)
  if progress_callback: progress_callback(0.22+0.12*min(1,len(sitemap_seen)/40),f"{retailer}: checking sitemap {len(sitemap_seen)} of up to 40")
  try:
   response=retailer_get(sitemap_url,headers=HEADERS,timeout=8)
   if not response.ok: continue
   root=ET.fromstring(response.content)
   locs=[(node.text or "").strip() for node in root.iter() if node.tag.lower().endswith("loc") and node.text]
   if root.tag.lower().endswith("sitemapindex"):
    preferred=[u for u in locs if any(term in u.lower() for term in ("product","catalog","item","pdp"))]
    sitemap_queue.extend((preferred or locs)[:8])
    continue
   for product_url in locs:
    if "pokemon" not in product_url.lower(): continue
    if not retailer_domain_match(product_url,domain): continue
    add_candidate(product_url,"Pokemon Trading Card Product")
    sitemap_product_count+=1
    if sitemap_product_count>=500: break
  except Exception as e:
   log.warning("Retailer sitemap unavailable for %s (%s): %s",retailer,sitemap_url,e)
 if sitemap_product_count:
  log.info("%s: %s Pokémon URLs found in retailer-owned sitemaps",retailer,sitemap_product_count)
 # Free, keyless Bing RSS discovery. This public search-results feed requires no account or key.
 # Results discover candidate URLs only; retailer pages remain the source of truth.
 search_terms=[
  "Pokemon TCG trading cards",
  "Pokemon Elite Trainer Box",
  "Pokemon booster bundle",
  "Pokemon 30th Celebration cards",
  "Pokemon Mega Evolution Pokemon TCG",
  "Pokemon trading card collection box"
 ]
 for term_index,term in enumerate(search_terms,1):
  query=f"site:{domain} {term}"
  try:
   if progress_callback: progress_callback(0.24+0.08*term_index/len(search_terms),f"{retailer}: searching public product results {term_index}/{len(search_terms)}")
   rss_url="https://www.bing.com/search?format=rss&count=50&q="+requests.utils.quote(query)
   sr=requests.get(rss_url,headers={**HEADERS,"Accept":"application/rss+xml, application/xml;q=0.9, */*;q=0.8"},timeout=12)
   sr.raise_for_status()
   root=ET.fromstring(sr.content)
   for item in root.findall(".//item"):
    result_title=(item.findtext("title") or "").strip()
    result_link=(item.findtext("link") or "").strip()
    result_desc=(item.findtext("description") or "").strip()
    candidates_from_result=[]
    if result_link: candidates_from_result.append(result_link)
    # Some RSS entries expose retailer links in description anchors.
    for raw in re.findall(r"""href=['"]([^'"]+)['"]""",result_desc,re.I):
     candidates_from_result.append(raw)
    for raw in candidates_from_result:
     raw=unquote(raw.replace("&amp;","&"))
     if "bing.com/ck/a" in raw:
      try:
       params=parse_qs(urlparse(raw).query)
       encoded=params.get("u",[""])[0]
       if encoded.startswith("a1"):
        import base64
        raw=base64.urlsafe_b64decode(encoded[2:]+"===" ).decode("utf-8","ignore")
      except Exception: pass
     raw=raw.split("&amp;")[0]
     add_candidate(raw,result_title)
   log.info("%s: Bing RSS query %s found %s total candidate URLs",retailer,term_index,len(found))
  except Exception as e:
   log.warning("%s: free Bing RSS discovery query failed (%s): %s",retailer,term_index,str(e)[:180])
 # Bing RSS can return an empty feed even when retailer product pages are indexed.
 # Use DuckDuckGo's public HTML results as a second independent, keyless discovery
 # source. Search results only nominate product URLs; the retailer page is still
 # fetched below and must pass the Pokémon TCG and seller checks before saving.
 ddg_terms=[
  "Pokemon trading card game",
  "Pokemon elite trainer box booster bundle",
  "Pokemon 30th celebration Mega Evolution TCG"
 ] if len(found)<10 else []
 for term_index,term in enumerate(ddg_terms,1):
  query=f"site:{domain} {term}"
  try:
   if progress_callback: progress_callback(0.32+0.06*term_index/len(ddg_terms),f"{retailer}: checking independent product search {term_index}/{len(ddg_terms)}")
   search_url="https://html.duckduckgo.com/html/?q="+requests.utils.quote(query)
   response=requests.get(search_url,headers={**HEADERS,"Referer":"https://duckduckgo.com/","Accept":"text/html,application/xhtml+xml"},timeout=15)
   response.raise_for_status()
   search_soup=BeautifulSoup(response.text,"html.parser")
   added_before=len(found)
   for anchor in search_soup.select("a.result__a, a[data-testid='result-title-a'], a[href]"):
    raw=anchor.get("href","").strip()
    if not raw: continue
    parsed=urlparse(raw)
    if "duckduckgo.com" in (parsed.hostname or ""):
     target=parse_qs(parsed.query).get("uddg",[""])[0]
     if target: raw=unquote(target)
    label=anchor.get_text(" ",strip=True)
    add_candidate(raw,label)
   log.info("%s: DuckDuckGo query %s discovered %s additional product URLs (%s total)",retailer,term_index,len(found)-added_before,len(found))
  except (requests.RequestException,ValueError) as e:
   log.warning("%s: independent product search query %s failed: %s",retailer,term_index,str(e)[:160])
 # Search-engine results supplement retailer catalog pages, sitemaps and curated seeds.
 products=[]; c=conn()
 priority=("30th","delta-reign","ascended-heroes","chaos-rising","pitch-black","prismatic","mega-evolution","151","charizard","booster-bundle","elite-trainer")
 candidate_limit=500 if retailer=="Target" else 150
 if max_candidates is not None: candidate_limit=max(1,min(candidate_limit,int(max_candidates)))
 candidates=sorted(found.items(),key=lambda item:(0 if any(term in (item[0]+" "+item[1]).lower() for term in priority) else 1))[:candidate_limit]
 def fetch_candidate(item):
  u,title=item
  try:
   page=retailer_get(u,headers=HEADERS,timeout=18)
   if (retailer=="Target" and ((not page.ok) or re.search(r"robot or human|captcha|verify you are human|access denied|unusual traffic",page.text[:30000],re.I))):
    rendered=jina_read_url(u,timeout=20)
    if rendered and rendered.ok:
     log.info("Target product page retrieved through rendered-page fallback: %s",u)
     return item,rendered
   if (not page.ok) or re.search(r"robot or human|captcha|verify you are human|access denied|unusual traffic",page.text[:30000],re.I):
    log.info("Retailer page blocked for %s; availability remains unknown",u)
    return item,None
   return item,page
  except Exception as e:
   log.debug("Retailer product fetch failed for %s: %s",u,e)
   return item,None
 fetched=[]
 if progress_callback: progress_callback(0.35,f"{retailer}: found {len(candidates)} product pages; checking availability")
 with ThreadPoolExecutor(max_workers=6) as pool:
  futures={pool.submit(fetch_candidate,item):item for item in candidates}
  for completed,future in enumerate(as_completed(futures),1):
   try: fetched.append(future.result())
   except Exception: fetched.append((futures[future],None))
   if progress_callback: progress_callback(0.35+0.55*completed/max(1,len(candidates)),f"{retailer}: checked {completed} of {len(candidates)} product pages")
 log.info("%s: checking %s retailer product pages from %s discovered candidates",retailer,len(candidates),len(found))
 for (candidate_index,((u,title),page)) in enumerate(fetched,1):
  try:
   if page is None:
    # A blocked page is not evidence of out-of-stock. Retain only candidates
    # whose independent catalog/search title identifies an actual TCG item.
    if is_pokemon_tcg_title(title) and title.lower() not in ("pokemon tcg elite trainer box","pokemon trading card product"):
     t=now()
     c.execute("""INSERT INTO products(retailer,title,url,availability,source,first_seen,last_seen,last_checked,
      seller_verified,seller_name,popularity_score,popularity_reason,discovery_sources,stock_checked_at)
      VALUES(?,?,?,'unknown','discovered URL; retailer page blocked',?,?,?,0,'Seller not verified',0,
      'Seller/product page not accessible','Retailer catalog/search result; product page blocked',?) ON CONFLICT(url) DO NOTHING""",
      (retailer,title,u,t,t,t,t))
     products.append({"retailer":retailer,"title":title,"url":u,"price":None,"availability":"unknown",
      "seller":None,"seller_verified":False,"popularity_score":0,"popularity_reason":"Seller/product page not accessible"})
     log.info("%s candidate %s retained as discovered/unverified: %s",retailer,candidate_index,title[:100])
    else:
     log.info("%s candidate %s: request failed; no trustworthy product title to retain",retailer,candidate_index)
    continue
   if not page.ok:
    log.info("%s candidate %s: retailer page HTTP %s; availability remains unknown",retailer,candidate_index,page.status_code)
    if is_pokemon_tcg_title(title) and title.lower() not in ("pokemon tcg elite trainer box","pokemon trading card product"):
     t=now()
     c.execute("""INSERT INTO products(retailer,title,url,availability,source,first_seen,last_seen,last_checked,
      seller_verified,seller_name,popularity_score,popularity_reason,discovery_sources,stock_checked_at)
      VALUES(?,?,?,'unknown','discovered URL; retailer page blocked',?,?,?,0,'Seller not verified',0,
      'Seller/product page not accessible','Retailer catalog/search result; product page blocked',?) ON CONFLICT(url) DO NOTHING""",
      (retailer,title,u,t,t,t,t))
     products.append({"retailer":retailer,"title":title,"url":u,"price":None,"availability":"unknown",
      "seller":None,"seller_verified":False,"popularity_score":0,"popularity_reason":"Seller/product page not accessible"})
    continue
   ps=BeautifulSoup(page.text,"html.parser")
   page_text=ps.get_text(" ",strip=True)
   embedded=" ".join(x.get_text(" ",strip=True) for x in ps.select("script[type='application/ld+json'],script#__NEXT_DATA__,script[type='application/json']"))
   verification_text=(page_text+" "+embedded)[:500000]
   og=ps.find("meta",property="og:title")
   if og and og.get("content"):
    title=og["content"][:250]
   elif ps.title:
    title=ps.title.get_text(" ",strip=True)[:250]
   elif "URL Source:" in page.text:
    heading=re.search(r"^#\s+(.+)$",page.text,re.M)
    title=heading.group(1).strip()[:250] if heading else ""
   else:
    title=""
   price=None
   # Read the actual Product/Offer price from JSON-LD where retailers publish it.
   def find_offer_price(obj):
    if isinstance(obj,dict):
     offers=obj.get("offers")
     if isinstance(offers,dict):
      val=offers.get("price") or offers.get("lowPrice")
      if val is not None: return str(val)
     if isinstance(offers,list):
      for offer in offers:
       if isinstance(offer,dict) and (offer.get("price") or offer.get("lowPrice")) is not None:
        return str(offer.get("price") or offer.get("lowPrice"))
     for value in obj.values():
      found_price=find_offer_price(value)
      if found_price: return found_price
    elif isinstance(obj,list):
     for value in obj:
      found_price=find_offer_price(value)
      if found_price: return found_price
    return None
   for node in ps.select('script[type="application/ld+json"]'):
    try:
     structured=json.loads(node.string or node.get_text())
     price=find_offer_price(structured)
     if price: break
    except Exception: pass
   if not price:
    for sel in ['meta[property="product:price:amount"]','meta[property="og:price:amount"]','meta[itemprop="price"]']:
     n=ps.select_one(sel)
     if n and n.get("content"):
      price=n["content"].strip()
      if re.fullmatch(r"\\d+(?:\\.\\d{1,2})?",price): break
      price=None
   if not price:
    price_match=re.search(r"(?:Price|Our price|Now)\\s*:?\\s*\\$\\s*(\\d{1,4}(?:,\\d{3})*(?:\\.\\d{2})?)",page_text,re.I)
    if not price_match: price_match=re.search(r"\\$\\s*(\\d{1,4}(?:,\\d{3})*\\.\\d{2})",page_text)
    if price_match: price=price_match.group(1).replace(",","")
   if not is_pokemon_tcg_title(title):
    log.info("%s candidate %s rejected: page title not a Pokémon TCG product (%s)",retailer,candidate_index,title[:100])
    continue
   verified,seller=verify_retailer_seller(retailer,verification_text)
   if not verified:
    log.info("%s candidate %s rejected: seller not verified as retailer (%s)",retailer,candidate_index,title[:100])
    continue
   stock=online_stock_status(page_text,embedded+" "+page.text,retailer)
   log.info("%s verified: %s | online=%s | price=%s",retailer,title[:100],stock,price)
   score,reason=popularity_for(title,verification_text)
   t=now()
   c.execute("""INSERT INTO products(retailer,title,url,price,availability,source,first_seen,last_seen,last_checked,seller_verified,seller_name,popularity_score,popularity_reason,discovery_sources,stock_checked_at)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,
    price=COALESCE(excluded.price,products.price),seller_verified=1,seller_name=excluded.seller_name,
    popularity_score=excluded.popularity_score,popularity_reason=excluded.popularity_reason,
    availability=excluded.availability,source=excluded.source,last_seen=excluded.last_seen,
    last_checked=excluded.last_checked,discovery_sources=excluded.discovery_sources,stock_checked_at=excluded.stock_checked_at""",
    (retailer,title,u,price,stock,"retailer product page",t,t,t,1,seller,score,reason,"Retailer catalog/sitemap + Bing RSS + DuckDuckGo + curated seeds",t))
   products.append({"retailer":retailer,"title":title,"url":u,"price":price,"availability":stock,"seller":seller,"seller_verified":True,"popularity_score":score,"popularity_reason":reason})
  except Exception as e:
   log.warning("Retailer product page parse failed for %s (%s): %s",retailer,u,e)
 c.commit(); c.close()
 log.info("%s: %s retailer-owned Pokémon TCG product pages verified from %s candidates",retailer,len(products),len(found))
 return products

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
def scan_store_inventory(progress_callback=None):
 c=conn()
 stores=[dict(x) for x in c.execute("SELECT * FROM stores ORDER BY retailer,distance")]
 products=[dict(x) for x in c.execute("SELECT * FROM products WHERE seller_verified=1 ORDER BY popularity_score DESC,last_seen DESC LIMIT 30")]
 checked=0; confirmed=0; unknown=0; errors=0; unsupported=0
 target_stores=[x for x in stores if x["retailer"]=="Target"]
 work_total=max(1,len(stores)+len(target_stores)*len(products)); work_done=0
 # Target begins rate-limiting this shared server after repeated product-page
 # requests. Once it returns 403/429 or Chromium crashes, stop requesting more
 # Target pages for this run instead of spending many minutes repeating failures.
 target_access_blocked=False
 target_access_block_reason=""
 for store in stores:
  if store["retailer"]!="Target":
   unsupported+=1
   detail="This retailer does not expose a supported branch-level stock source yet; online listings are not counted as local stock."
   c.execute("INSERT INTO store_inventory_checks(store_id,status,checked_at,detail) VALUES(?,?,?,?) ON CONFLICT(store_id) DO UPDATE SET status=excluded.status,checked_at=excluded.checked_at,detail=excluded.detail",(store["id"],"unsupported",now(),detail))
   c.commit(); work_done+=1
   if progress_callback: progress_callback(work_done,work_total,"Skipping unsupported branch-level source")
   continue
  branch_rows=[]; failures=0
  for product in products:
   if target_access_blocked:
    work_done+=1
    if progress_callback: progress_callback(work_done,work_total,f"Target checks paused: {target_access_block_reason}")
    continue
   try:
    page=browser_get(product["url"],timeout=25,store=store)
    if not page.ok: raise requests.HTTPError(f"Retailer page HTTP {page.status_code}")
    soup=BeautifulSoup(page.text,"html.parser")
    visible=re.sub(r"\s+"," ",soup.get_text(" ",strip=True)).lower()
    street=(store.get("address") or "").split(",")[0].lower()
    street_token=street.split()[0] if street else ""
    branch_name=(store.get("name") or "").lower()
    zip_code=str(store.get("postcode") or "")
    branch_context=(branch_name and branch_name in visible) or (street_token and street_token in visible and zip_code and zip_code in visible)
    status,quantity,label=branch_stock_status(visible)
    if not branch_context:
     status="unknown"; quantity=None
     evidence="Rendered product page did not visibly confirm this exact branch; stock left unknown"
    else:
     evidence=f"Rendered Target product page; branch context visible; {label}"
    branch_rows.append((product["url"],product["title"],status,quantity,evidence))
   except Exception as e:
    failures+=1
    error_text=str(e)
    log.warning("Target browser check failed for %s at %s: %s",product.get("title"),store.get("name"),error_text[:180])
    if "HTTP 403" in error_text or "HTTP 429" in error_text or "Target crashed" in error_text:
     target_access_blocked=True
     target_access_block_reason=("Target returned an access/rate-limit response" if ("HTTP 403" in error_text or "HTTP 429" in error_text) else "Target's browser renderer crashed")
   finally:
    work_done+=1
    if progress_callback: progress_callback(work_done,work_total,f"Checking {store.get('name','Target branch')} — {product.get('title','product')}")
  if not products:
   work_done+=1
   if progress_callback: progress_callback(work_done,work_total,f"No product pages available for {store.get('name','Target branch')}")
  if branch_rows:
   c.execute("DELETE FROM store_inventory WHERE store_id=?",(store["id"],))
  for url,title,status,quantity,evidence in branch_rows:
   checked_at=now()
   label=(f"Retailer-reported units: {quantity}" if quantity is not None else "Count unavailable")
   c.execute("""INSERT INTO store_inventory(store_id,product_url,product_title,status,quantity,quantity_label,source_url,detail,checked_at)
    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(store_id,product_url) DO UPDATE SET product_title=excluded.product_title,status=excluded.status,quantity=excluded.quantity,quantity_label=excluded.quantity_label,source_url=excluded.source_url,detail=excluded.detail,checked_at=excluded.checked_at""",
    (store["id"],url,title,status,quantity,label,url,evidence,checked_at))
   if status=="in_stock": confirmed+=1
   elif status=="unknown": unknown+=1
  state_value="checked" if branch_rows and failures==0 else ("partial" if branch_rows else "error")
  detail=f"Chromium browser scan: {sum(1 for x in branch_rows if x[2]=='in_stock')} in stock; {len(branch_rows)} product pages visited; {failures} browser errors."
  c.execute("INSERT INTO store_inventory_checks(store_id,status,checked_at,detail) VALUES(?,?,?,?) ON CONFLICT(store_id) DO UPDATE SET status=excluded.status,checked_at=excluded.checked_at,detail=excluded.detail",(store["id"],state_value,now(),detail))
  c.commit(); checked+=1
 c.close()
 return {"confirmed_products":confirmed,"message":f"Browser-based retailer scan: {checked} Target branches visited; {confirmed} confirmed product/store listings in stock; {unknown} unknown; {errors} branch errors; {unsupported} other retailer branches not yet supported."}

def update_scan_progress(run_id, progress, phase, current=0, total=0):
 c=conn()
 c.execute("UPDATE poketrack_scan_runs SET progress=?,phase=?,current=?,total=? WHERE id=? AND status='running'",
  (max(0,min(99,int(progress))),str(phase)[:240],int(current),int(total),run_id))
 c.commit();c.close()

def run_scan(kind):
 global JINA_CALLS
 if not SCAN_LOCK.acquire(blocking=False):
  return {"status":"busy","message":"A scan is already running. Please wait for it to finish."},409
 JINA_CALLS=0
 try:
  c=conn(); cur=c.execute("INSERT INTO poketrack_scan_runs(kind,started_at,status,progress,phase) VALUES(?,?,?,0,?)",(kind,now(),"running","Starting scan")); rid=cur.lastrowid;c.commit();c.close()
 except Exception:
  SCAN_LOCK.release()
  raise
 total=0; notes=[]; retailer_failures=[]
 try:
  if kind in ("all","stores"):
   update_scan_progress(rid,1,"Discovering physical store locations",0,1)
   n=discover_stores();total+=n;notes.append(f"{n} physical store locations discovered")
   update_scan_progress(rid,10 if kind=="all" else 95,"Physical store directory refreshed",1,1)
  if kind in ("all","products"):
   retailer_names=list(RETAILERS)
   for idx,retailer in enumerate(retailer_names,1):
    phase_pct=(10+int(idx*60/len(retailer_names))) if kind=="all" else int(idx*95/len(retailer_names))
    update_scan_progress(rid,phase_pct-1,f"Scanning {retailer} product listings",idx-1,len(retailer_names))
    status_conn=conn()
    status_conn.execute("INSERT INTO retailer_scan_status(retailer,status,checked_at,product_count,detail) VALUES(?,?,?,?,?) ON CONFLICT(retailer) DO UPDATE SET status=excluded.status,checked_at=excluded.checked_at,product_count=excluded.product_count,detail=excluded.detail",(retailer,"running",now(),0,"Scan in progress"))
    status_conn.commit();status_conn.close()
    try:
     def retailer_progress(fraction,detail):
      bounded=max(0.0,min(0.99,float(fraction)))
      pct=10+int(60*(idx-1+bounded)/len(retailer_names)) if kind=="all" else int(95*(idx-1+bounded)/len(retailer_names))
      update_scan_progress(rid,pct,detail,idx-1,len(retailer_names))
     retailer_products=search_products(retailer,progress_callback=retailer_progress)
     n=len(retailer_products); verified_n=sum(1 for p in retailer_products if p.get("seller_verified"))
     total+=n;notes.append(f"{retailer}: {n} discovered listings ({verified_n} first-party verified)")
     retailer_status="successful" if n and verified_n==n else ("partial" if n else "unsuccessful")
     retailer_detail=f"{n} discovered listings; {verified_n} first-party verified; {n-verified_n} unverified/blocked; availability unknown unless explicitly confirmed"
    except Exception as e:
     retailer_failures.append(retailer)
     notes.append(f"{retailer}: FAILED — {str(e)[:100]}")
     log.exception("Product listing scan failed for %s",retailer)
     n=0;retailer_status="unsuccessful";retailer_detail=str(e)[:500]
    status_conn=conn()
    status_conn.execute("UPDATE retailer_scan_status SET status=?,checked_at=?,product_count=?,detail=? WHERE retailer=?",(retailer_status,now(),n,retailer_detail,retailer))
    status_conn.commit();status_conn.close()
    update_scan_progress(rid,phase_pct,f"{retailer}: scan finished"+(" with errors" if retailer in retailer_failures else ""),idx,len(retailer_names))
  if kind in ("all","inventory"):
   base=70 if kind=="all" else 0
   span=29 if kind=="all" else 95
   def inventory_progress(current,work_total,phase):
    pct=base+int(span*current/max(1,work_total))
    update_scan_progress(rid,pct,phase,current,work_total)
   result=scan_store_inventory(progress_callback=inventory_progress);total+=result["confirmed_products"];notes.append(result["message"])
  msg="; ".join(notes)
  final_status="partial" if retailer_failures else "completed"
  final_phase="Completed with retailer errors" if retailer_failures else "Completed"
  c=conn();c.execute("UPDATE poketrack_scan_runs SET finished_at=?,status=?,found=?,message=?,progress=100,phase=?,current=1,total=1 WHERE id=?",(now(),final_status,total,msg,final_phase,rid));c.commit();c.close()
  return {"status":final_status,"found":total,"message":msg}
 except Exception as e:
  log.exception("scan failed");c=conn();c.execute("UPDATE poketrack_scan_runs SET finished_at=?,status='error',found=?,message=?,phase='Failed' WHERE id=?",(now(),total,str(e)[:500],rid));c.commit();c.close()
  return {"status":"error","found":total,"message":str(e)},500
 finally:
  # Playwright's synchronous API is thread-affine. The scan runs in a
  # short-lived background thread, so never retain its browser/greenlet
  # globals for the next scan thread.
  try:
   _close_browser()
  except Exception as cleanup_error:
   log.warning("Browser cleanup after scan failed: %s",cleanup_error)
  SCAN_LOCK.release()
HTML="""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PokeTrack Radar</title><style>
:root{color-scheme:dark;--bg:#090e19;--panel:#121b2b;--line:#26364d;--muted:#9aabc2;--accent:#62d6c5;--gold:#f5c76b}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#edf4ff;font:15px system-ui}header{padding:24px max(18px,calc((100% - 1240px)/2));background:#101a2a;border-bottom:1px solid var(--line)}h1{margin:0;font-size:26px}header p,.muted{color:var(--muted)}main{max-width:1240px;margin:auto;padding:20px}.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.panel,.stat{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}.stat b{font-size:25px;display:block}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}.toolbar input,.toolbar select,button{background:#1b2940;color:#fff;border:1px solid #3b506c;border-radius:9px;padding:11px}button{cursor:pointer;background:#176e68;border:0;font-weight:700}.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.tabs button{background:#24344d}.tabs button.active{background:#176e68}.item{padding:12px 0;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:12px}.item a{color:var(--accent)}.pill{border:1px solid #465b74;border-radius:20px;padding:3px 8px;font-size:12px;white-space:nowrap}.ok{color:#8ee0a9}.warn{color:var(--gold)}.scan-progress{margin-top:14px;padding:12px;background:#0d1625;border:1px solid var(--line);border-radius:10px}.scan-progress-head{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:8px}.progress-track{height:12px;background:#26364d;border-radius:20px;overflow:hidden}.progress-fill{height:100%;width:0;background:var(--accent);transition:width .35s ease;border-radius:20px}.progress-meta{display:flex;justify-content:space-between;gap:10px;margin-top:7px;font-size:12px;color:var(--muted)}@media(max-width:760px){.grid{grid-template-columns:repeat(2,1fr)}.item{display:block}.item .pill{display:inline-block;margin-top:6px}}
</style></head><body><header><h1>⚡ PokeTrack Radar</h1><p>Physical store discovery · Pokémon product listings · Winchester, VA 22601 · 80-mile radius</p></header><main>
<section class="panel"><h2>Retailer scan status</h2><p class="muted">Latest product-listing scan result for each retailer. Discovered listings include unverified candidates; seller verification and stock status are tracked separately. Blocked pages remain unknown.</p><div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;text-align:left"><thead><tr><th style="padding:10px;border-bottom:1px solid var(--line)">Business</th><th style="padding:10px;border-bottom:1px solid var(--line)">Scan status</th><th style="padding:10px;border-bottom:1px solid var(--line)">Discovered listings</th><th style="padding:10px;border-bottom:1px solid var(--line)">Last checked</th></tr></thead><tbody id="retailerScanRows"><tr><td colspan="4" class="muted" style="padding:10px">No retailer scan results yet.</td></tr></tbody></table></div></section>
<div class="grid"><div class="stat"><b id="stores">—</b><span class="muted">Physical stores found</span></div><div class="stat"><b id="products">—</b><span class="muted">Product listings</span></div><div class="stat"><b id="stock">—</b><span class="muted">In-store products confirmed</span></div><div class="stat"><b id="onlineStock">—</b><span class="muted">Verified online products in stock</span></div><div class="stat"><b id="last">—</b><span class="muted">Last completed scan</span></div></div>
<section class="panel"><h2>Radar controls</h2><p class="muted">Store locations come from a researched retailer branch directory. Online stock is counted separately from physical-store stock. Online counts include only verified retailer-owned product pages that explicitly report online availability. Physical-store counts require branch-specific evidence; blocked or unconfirmed results remain unknown.</p><div class="toolbar"><button onclick="scan('all')">Run full scan</button><button onclick="scan('stores')">Refresh store directory</button><button onclick="scan('products')">Scan product listings</button><button onclick="scan('inventory')">Browser-based store stock scan</button><span id="msg" class="muted">Ready</span></div><div class="scan-progress"><div class="scan-progress-head"><b id="progressPhase">No scan running</b><b id="progressPercent">0%</b></div><div class="progress-track"><div id="progressFill" class="progress-fill"></div></div><div class="progress-meta"><span id="progressCount">Waiting for scan</span><span id="progressState">Idle</span></div></div></section>
<div class="tabs"><button class="active" onclick="tab('stores',this)">Physical stores</button><button onclick="tab('products',this)">Product listings</button><button onclick="tab('history',this)">Scan history</button></div>
<section class="panel"><div class="toolbar"><input id="q" placeholder="Filter by store, city, product…" oninput="draw()"><select id="retailer" onchange="draw()"><option value="">All retailers</option></select><select id="availability" onchange="draw()"><option value="">All availability</option><option value="in_stock">Explicitly in stock</option><option value="out_of_stock">Explicitly sold out</option><option value="unknown">Unknown</option></select></div><div id="list" class="muted">Loading…</div></section>
<p class="muted">Store locations are loaded from the researched branch directory. No map service is used. Product links open retailer websites. Availability can change and must be confirmed with the retailer.</p></main><script>
let mode='stores',stores=[],products=[],history=[],inventory=[],retailerScanStatuses=[];function tab(m,b){mode=m;document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('active'));b.classList.add('active');draw()}
async function load(){try{let [a,b,c,d,e,f]=await Promise.all(['/api/stores','/api/products','/api/history','/api/summary','/api/inventory','/api/retailer-scan-status'].map(x=>fetch(x).then(r=>r.json())));stores=a.stores||[];products=b.products||[];history=c.runs||[];inventory=e.inventory||[];retailerScanStatuses=f.retailers||[];renderRetailerScanStatuses();renderProgress();document.getElementById('stores').textContent=stores.length;document.getElementById('products').textContent=products.length;document.getElementById('stock').textContent=inventory.filter(x=>x.status==='in_stock').length;document.getElementById('onlineStock').textContent=products.filter(x=>x.seller_verified===1&&x.availability==='in_stock').length;document.getElementById('last').textContent=d.last_scan?new Date(d.last_scan).toLocaleString():'—';let opts=[...new Set([...stores,...products].map(x=>x.retailer))].sort();document.getElementById('retailer').innerHTML='<option value="">All retailers</option>'+opts.map(x=>'<option>'+esc(x)+'</option>').join('');draw()}catch(e){document.getElementById('list').textContent='Could not load dashboard data: '+e}}
function renderRetailerScanStatuses(){let el=document.getElementById('retailerScanRows');if(!retailerScanStatuses.length){el.innerHTML='<tr><td colspan="4" class="muted" style="padding:10px">No retailer scan results yet. Run a product-listings scan.</td></tr>';return}el.innerHTML=retailerScanStatuses.map(x=>'<tr><td style="padding:10px;border-bottom:1px solid var(--line)"><b>'+esc(x.retailer)+'</b></td><td style="padding:10px;border-bottom:1px solid var(--line)"><span class="pill '+(x.status==='successful'?'ok':x.status==='unsuccessful'?'warn':'')+'">'+(x.status==='successful'?'Successful':x.status==='unsuccessful'?'Unsuccessful':esc(x.status))+'</span></td><td style="padding:10px;border-bottom:1px solid var(--line)">'+Number(x.product_count||0)+'</td><td style="padding:10px;border-bottom:1px solid var(--line)">'+esc(x.checked_at?new Date(x.checked_at).toLocaleString():'—')+'</td></tr>').join('')}
function renderProgress(){let run=history.find(x=>x.status==='running')||history[0];let active=run&&run.status==='running';let pct=run?Number(run.progress||0):0;document.getElementById('progressFill').style.width=pct+'%';document.getElementById('progressPercent').textContent=pct+'%';document.getElementById('progressPhase').textContent=run?(run.phase|| (active?'Scan running':'Last scan')):'No scan running';document.getElementById('progressCount').textContent=run&&Number(run.total)>0?(Number(run.current||0)+' / '+Number(run.total)+' steps'):(active?'Working…':'Waiting for scan');document.getElementById('progressState').textContent=active?'In progress':run?run.status:'Idle';if(run&&run.status==='completed'){document.getElementById('progressFill').style.width='100%';document.getElementById('progressPercent').textContent='100%'}}
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function draw(){let q=document.getElementById('q').value.toLowerCase(),r=document.getElementById('retailer').value,a=document.getElementById('availability').value;let rows=mode==='stores'?[...stores].sort((a,b)=>Number(b.in_stock_products||0)-Number(a.in_stock_products||0)||Number(b.online_in_stock_products||0)-Number(a.online_in_stock_products||0)||Number(a.distance||0)-Number(b.distance||0)):mode==='products'?products:history;rows=rows.filter(x=>(!r||x.retailer===r)&&(!a||x.availability===a)&&JSON.stringify(x).toLowerCase().includes(q));document.getElementById('list').innerHTML=rows.length?rows.map(x=>mode==='stores'?'<div class="item"><div><b>'+esc(x.name)+'</b><div class="muted">'+esc(x.retailer)+' · '+esc(x.address||x.city||'Address not listed')+'</div><div class="muted">'+Number(x.distance).toFixed(1)+' miles from 22601 · <a href="'+esc(x.source_url)+'" target="_blank">Store page</a></div><div class="grid" style="grid-template-columns:repeat(2,minmax(0,1fr));margin:12px 0"><div class="stat"><b>'+(x.inventory_check_status==='checked'||x.inventory_check_status==='partial'?Number(x.in_stock_products||0):'—')+'</b><span class="muted">In-store products confirmed at this branch</span><div class="muted">'+esc(x.inventory_check_status==='unsupported'?'Branch check not supported':x.inventory_check_status==='error'?'Branch check failed':x.inventory_check_status==='checked'||x.inventory_check_status==='partial'?'Based on branch-specific checks':'Not checked')+'</div></div><div class="stat"><b>'+Number(x.online_in_stock_products||0)+'</b><span class="muted">Online products in stock</span><div class="muted">Retailer-wide · '+Number(x.online_listings||0)+' verified listings; not branch-specific</div></div></div><details><summary>Online inventory · '+Number(x.online_in_stock_products||0)+' in stock / '+Number(x.online_listings||0)+' total retailer listings</summary>'+((products.filter(p=>p.retailer===x.retailer&&p.availability==='in_stock').length)?products.filter(p=>p.retailer===x.retailer&&p.availability==='in_stock').map(p=>'<div class="muted"><a href="'+esc(p.url)+'" target="_blank">'+esc(p.title)+'</a> · '+esc(p.price||'Price not listed')+' · In stock online</div>').join(''):'<div class="muted">No verified online in-stock listings currently recorded for this retailer.</div>')+'</details><details><summary>View branch inventory</summary>'+((inventory.filter(y=>y.store_id===x.id).length)?inventory.filter(y=>y.store_id===x.id).map(y=>'<div class="muted">'+esc(y.product_title)+' — '+(y.status==='in_stock'?'In stock — '+esc(y.quantity_label):y.status==='out_of_stock'?'Out of stock':'Unknown')+' · '+esc(y.checked_at||'')+' · <a href="'+esc(y.source_url)+'" target="_blank">Retailer product page</a><div class="muted">'+esc(y.detail||'')+'</div></div>').join(''):'<div class="muted">No verified branch-specific product results. This does not mean the store is out of stock.</div>')+'</details></div><span class="pill">'+esc(x.inventory_check_time?'Checked '+new Date(x.inventory_check_time).toLocaleString():'Not checked')+'</span></div>':mode==='products'?'<div class="item"><div><b><a href="'+esc(x.url)+'" target="_blank">'+esc(x.title)+'</a></b><div class="muted">'+esc(x.retailer)+' · '+esc(x.price||'Price not listed')+' · '+esc(x.seller_name||'Seller not verified')+' · '+(x.seller_verified?'First-party verified':'Unverified listing')+'</div><div class="muted">Checked '+esc(x.stock_checked_at||x.last_checked||'unknown')+'</div></div><span class="pill">'+(x.availability==='in_stock'?'In stock':x.availability==='out_of_stock'?'Out of stock':'Unknown')+'</span></div>':'<div class="item"><div><b>'+esc(x.kind)+' · '+esc(x.status)+'</b><div class="muted">'+esc(x.message)+'</div></div><span class="pill">'+esc(x.started_at)+'</span></div>').join(''):'<p class="muted">No matching records yet. Run a scan to populate this section.</p>'}
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
 c=conn();rows=[dict(x) for x in c.execute("""SELECT s.*,COALESCE(SUM(CASE WHEN i.status='in_stock' THEN 1 ELSE 0 END),0) AS in_stock_products,COUNT(i.product_url) AS branch_product_results,(SELECT COUNT(*) FROM products p WHERE p.retailer=s.retailer AND p.seller_verified=1 AND p.availability='in_stock') AS online_in_stock_products,(SELECT COUNT(*) FROM products p WHERE p.retailer=s.retailer AND p.seller_verified=1) AS online_listings,MAX(i.checked_at) AS inventory_checked_at,MAX(k.status) AS inventory_check_status,MAX(k.checked_at) AS inventory_check_time FROM stores s LEFT JOIN store_inventory i ON i.store_id=s.id LEFT JOIN store_inventory_checks k ON k.store_id=s.id GROUP BY s.id ORDER BY s.distance,s.retailer""")];c.close();return jsonify(stores=rows)
@app.get("/api/retailer-scan-status")
def api_retailer_scan_status():
 c=conn();rows=[dict(x) for x in c.execute("SELECT retailer,status,checked_at,product_count,detail FROM retailer_scan_status ORDER BY retailer")];c.close();return jsonify(retailers=rows)

@app.get("/api/inventory")
def api_inventory():
 c=conn();rows=[dict(x) for x in c.execute("""SELECT i.*,s.name AS store_name,s.retailer,s.address,s.city,s.state FROM store_inventory i JOIN stores s ON s.id=i.store_id ORDER BY s.distance,s.retailer,s.name,i.product_title""")];c.close();return jsonify(inventory=rows)
@app.get("/api/products")
def api_products():
 c=conn();rows=[dict(x) for x in c.execute("SELECT * FROM products ORDER BY seller_verified DESC,popularity_score DESC,last_seen DESC LIMIT 2000")];c.close();return jsonify(products=rows)
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
