import os
import re
import json
import time
import sqlite3
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("poketrack")

DB_PATH = os.getenv("DB_PATH", "/data/poketrack.db")
ZIP_CODE = os.getenv("START_ZIP", "22601")
RADIUS_MILES = int(os.getenv("RADIUS_MILES", "200"))
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

# Retailer domains are allow-listed so search results from marketplaces and
# unrelated sites are not accidentally presented as retailer inventory.
RETAILERS = {
    "Target": ["target.com"],
    "Walmart": ["walmart.com"],
    "Best Buy": ["bestbuy.com"],
    "Amazon": ["amazon.com"],
    "Barnes & Noble": ["barnesandnoble.com"],
    "Costco": ["costco.com"],
    "Sam's Club": ["samsclub.com"],
    "Walgreens": ["walgreens.com"],
    "Dollar General": ["dollargeneral.com"],
    "Hobby Lobby": ["hobbylobby.com"],
    "Books-A-Million": ["booksamillion.com"],
    "Gabe's": ["gabesstores.com"],
}
SEARCH_TERMS = [
    '"Pokemon trading card game"',
    "Pokemon TCG booster bundle",
    "Pokemon Elite Trainer Box",
    "Pokemon booster box",
    "Pokemon collection box tin",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  retailer TEXT NOT NULL,
  title TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  price TEXT,
  availability TEXT NOT NULL DEFAULT 'unknown',
  source TEXT NOT NULL DEFAULT 'search_result',
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  last_checked TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  retailer TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  found INTEGER NOT NULL DEFAULT 0,
  message TEXT
);
"""

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def db():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn

def clean_url(url):
    url = unquote(url or "").strip()
    if url.startswith("/url?"):
        query = parse_qs(urlparse(url).query)
        url = query.get("q", query.get("url", [url]))[0]
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    host = parsed.netloc.lower().split(":")[0]
    if host.endswith("google.com") or host.endswith("bing.com"):
        return None
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

def belongs_to(url, domains):
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)

def search_web(query):
    """Use public search result HTML; no paid API key is required."""
    results = []
    endpoints = [
        ("https://www.google.com/search", {"q": query, "num": "20"}),
        ("https://www.bing.com/search", {"q": query, "count": "20"}),
    ]
    for endpoint, params in endpoints:
        try:
            response = requests.get(endpoint, params=params, headers=HEADERS, timeout=18)
            if response.status_code != 200:
                log.warning("Search returned HTTP %s for %s", response.status_code, endpoint)
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                if endpoint.startswith("https://www.bing.com"):
                    # Bing result links are normally nested under li.b_algo.
                    parent = a.find_parent("li", class_="b_algo")
                    if not parent:
                        continue
                    heading = parent.find(["h2", "h3"])
                    title = heading.get_text(" ", strip=True) if heading else a.get_text(" ", strip=True)
                    snippet_node = parent.select_one(".b_caption p")
                    snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
                else:
                    title = a.get_text(" ", strip=True)
                    snippet = ""
                    parent = a.find_parent("div")
                    if parent:
                        snippet = parent.get_text(" ", strip=True)[:500]
                url = clean_url(href)
                if url and title:
                    results.append({"title": title[:300], "url": url, "snippet": snippet[:500]})
        except requests.RequestException as exc:
            log.warning("Search request failed: %s", exc)
        if results:
            break
        time.sleep(0.6)
    # De-duplicate URLs while preserving result order.
    seen, unique = set(), []
    for item in results:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
    return unique

def page_details(url, fallback_title="", fallback_snippet=""):
    title, price, availability = fallback_title, None, "unknown"
    source = "search_result"
    try:
        response = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        if response.status_code == 200 and "text/html" in response.headers.get("Content-Type", ""):
            soup = BeautifulSoup(response.text, "html.parser")
            og = soup.find("meta", property="og:title")
            if og and og.get("content"):
                title = og["content"].strip()[:300]
            elif soup.title and soup.title.string:
                title = soup.title.string.strip()[:300]
            for selector in [
                'meta[property="product:price:amount"]',
                'meta[itemprop="price"]',
                'meta[property="og:price:amount"]',
            ]:
                node = soup.select_one(selector)
                if node and node.get("content"):
                    price = node["content"].strip()
                    break
            body = soup.get_text(" ", strip=True).lower()
            if re.search(r"\b(out of stock|sold out|currently unavailable|not available)\b", body):
                availability = "out_of_stock"
            elif re.search(r"\b(add to cart|ship it|pickup|in stock|available now)\b", body):
                availability = "in_stock"
            source = "product_page"
    except requests.RequestException as exc:
        log.info("Product page unavailable %s: %s", url, exc)
    # Search snippets are supporting evidence only. Do not infer stock from a
    # generic product listing or a search result with no explicit stock wording.
    evidence = (fallback_snippet or "").lower()
    if availability == "unknown":
        if re.search(r"\b(out of stock|sold out|currently unavailable)\b", evidence):
            availability = "out_of_stock"
        elif re.search(r"\b(in stock|available now)\b", evidence):
            availability = "in_stock"
    return title or "Pokémon TCG listing", price, availability, source

def save_product(retailer, item):
    now = utcnow()
    conn = db()
    try:
        conn.execute(
            """INSERT INTO products
            (retailer,title,url,price,availability,source,first_seen,last_seen,last_checked)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
              retailer=excluded.retailer,
              title=excluded.title,
              price=COALESCE(excluded.price, products.price),
              availability=excluded.availability,
              source=excluded.source,
              last_seen=excluded.last_seen,
              last_checked=excluded.last_checked""",
            (retailer, item["title"], item["url"], item.get("price"),
             item["availability"], item["source"], now, now, now),
        )
        conn.commit()
    finally:
        conn.close()

def scan_retailer(retailer):
    if retailer not in RETAILERS:
        raise ValueError("Unknown retailer")
    started = utcnow()
    conn = db()
    cur = conn.execute(
        "INSERT INTO scan_runs(retailer,started_at,status) VALUES(?,?,?)",
        (retailer, started, "running"),
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    found = 0
    try:
        domains = RETAILERS[retailer]
        for term in SEARCH_TERMS:
            query = f"site:{domains[0]} {term}"
            for result in search_web(query):
                url = result["url"]
                if not belongs_to(url, domains):
                    continue
                # Skip obvious category/search pages where possible, but keep
                # retailer product URLs even when the page cannot be parsed.
                title, price, availability, source = page_details(
                    url, result["title"], result["snippet"]
                )
                if not re.search(r"pokemon|pokémon|trading card|tcg", title + " " + result["snippet"], re.I):
                    continue
                save_product(retailer, {
                    "title": title, "url": url, "price": price,
                    "availability": availability, "source": source,
                })
                found += 1
        conn = db()
        conn.execute(
            "UPDATE scan_runs SET finished_at=?,status=?,found=?,message=? WHERE id=?",
            (utcnow(), "completed", found, "Scan finished; unknown means stock could not be verified.", run_id),
        )
        conn.commit()
        conn.close()
        return {"retailer": retailer, "status": "completed", "found": found}
    except Exception as exc:
        log.exception("Scan failed for %s", retailer)
        conn = db()
        conn.execute(
            "UPDATE scan_runs SET finished_at=?,status=?,found=?,message=? WHERE id=?",
            (utcnow(), "error", found, str(exc)[:500], run_id),
        )
        conn.commit()
        conn.close()
        return {"retailer": retailer, "status": "error", "found": found, "error": str(exc)}

@app.get("/")
def index():
    return jsonify({
        "name": "PokeTrack",
        "status": "online",
        "start_zip": ZIP_CODE,
        "radius_miles": RADIUS_MILES,
        "retailers": list(RETAILERS),
        "endpoints": ["/health", "/api/scan", "/api/scan-retailer?name=Target",
                      "/api/products", "/api/retailer-status", "/api/history"],
    })

@app.get("/health")
def health():
    try:
        conn = db()
        conn.execute("SELECT 1")
        conn.close()
        return jsonify({"ok": True, "time": utcnow()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

@app.post("/api/scan")
def scan_all():
    # Optional shared secret protects scan endpoints when deployed publicly.
    secret = os.getenv("SCAN_TOKEN")
    if secret and request.headers.get("X-Scan-Token") != secret:
        return jsonify({"error": "Unauthorized"}), 401
    results = [scan_retailer(name) for name in RETAILERS]
    return jsonify({"results": results, "total_found": sum(x["found"] for x in results)})

@app.route("/api/scan-retailer", methods=["GET", "POST"])
def scan_one():
    secret = os.getenv("SCAN_TOKEN")
    if secret and request.headers.get("X-Scan-Token") != secret:
        return jsonify({"error": "Unauthorized"}), 401
    name = request.args.get("name") or (request.json or {}).get("name")
    if not name:
        return jsonify({"error": "Pass retailer name in ?name=..."}), 400
    try:
        return jsonify(scan_retailer(name))
    except ValueError as exc:
        return jsonify({"error": str(exc), "retailers": list(RETAILERS)}), 400

@app.get("/api/products")
def products():
    conn = db()
    retailer = request.args.get("retailer")
    status = request.args.get("availability")
    limit = min(max(int(request.args.get("limit", "500")), 1), 2000)
    sql, params = "SELECT * FROM products WHERE 1=1", []
    if retailer:
        sql += " AND retailer=?"; params.append(retailer)
    if status:
        sql += " AND availability=?"; params.append(status)
    sql += " ORDER BY last_checked DESC LIMIT ?"; params.append(limit)
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return jsonify({"count": len(rows), "products": rows})

@app.get("/api/retailer-status")
def retailer_status():
    conn = db()
    rows = conn.execute("""
      SELECT retailer, MAX(started_at) AS last_started,
             MAX(finished_at) AS last_finished,
             (SELECT status FROM scan_runs s2 WHERE s2.retailer=s.retailer
              ORDER BY id DESC LIMIT 1) AS status,
             (SELECT found FROM scan_runs s3 WHERE s3.retailer=s.retailer
              ORDER BY id DESC LIMIT 1) AS found
      FROM scan_runs s GROUP BY retailer ORDER BY retailer
    """).fetchall()
    conn.close()
    return jsonify({"retailers": [dict(r) for r in rows]})

@app.get("/api/history")
def history():
    conn = db()
    rows = conn.execute("SELECT * FROM scan_runs ORDER BY id DESC LIMIT 200").fetchall()
    conn.close()
    return jsonify({"runs": [dict(r) for r in rows]})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
