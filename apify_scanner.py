"""Apify browser-based retailer discovery for PokeTrack.

Uses Apify's maintained Web Scraper Actor to render retailer catalog and
product pages. The API token is read only from APIFY_API_TOKEN; never log it.
"""
import logging
import os
import re
import time
from urllib.parse import urlparse

import requests

log = logging.getLogger("poketrack.apify")
API = "https://api.apify.com/v2"


def _is_tcg_title(value):
    title = re.sub(r"\s+", " ", str(value or "")).lower()
    if not re.search(r"pok[eé]mon", title):
        return False
    if re.search(r"\b(book|paperback|hardcover|story|stories|novel|guide|calendar|figure|plush|costume|apparel|backpack|video game|nintendo switch)\b", title):
        return False
    return bool(re.search(r"trading card|\btc[g]?\b|booster|elite trainer|\betb\b|blister|tin|deck|card pack|card game|pok[eé]mon cards", title))


PAGE_FUNCTION = r"""
async function pageFunction(context) {
    const { request, page, log } = context;
    const retailer = (context.customData && context.customData.retailer) || "";
    const domain = ((context.customData && context.customData.domain) || "").toLowerCase();
    const strictSeller = ["Target", "Walmart", "Best Buy"].includes(retailer);
    const data = request.userData || {};
    const kind = data.kind || "catalog";
    const tcg = (s) => {
        const t = String(s || "").replace(/\s+/g, " ").toLowerCase();
        if (!/pok[eé]mon/.test(t)) return false;
        if (/\b(book|paperback|hardcover|story|stories|novel|guide|calendar|figure|plush|costume|apparel|backpack|video game|nintendo switch)\b/.test(t)) return false;
        return /trading card|\btc[g]?\b|booster|elite trainer|\betb\b|blister|tin|deck|card pack|card game|pok[eé]mon cards/.test(t);
    };
    const absolute = (href) => { try { return new URL(href, request.url).href; } catch (_) { return ""; } };
    const sameRetailer = (url) => {
        try { const h = new URL(url).hostname.toLowerCase(); return h === domain || h.endsWith("." + domain); }
        catch (_) { return false; }
    };
    const pageText = (document.body && document.body.innerText || "").slice(0, 30000);
    if (kind === "catalog") {
        const anchors = Array.from(document.querySelectorAll("a[href]"));
        let queued = 0;
        for (const a of anchors) {
            const href = absolute(a.getAttribute("href"));
            if (!href || !sameRetailer(href)) continue;
            const img = a.querySelector("img");
            const label = [a.innerText, a.getAttribute("aria-label"), a.getAttribute("title"),
                img && img.alt, a.parentElement && a.parentElement.innerText].filter(Boolean).join(" ").slice(0, 500);
            if (!tcg(label)) continue;
            if (/\/(search|browse|category|collection|department)\b/i.test(new URL(href).pathname)) continue;
            await context.enqueueRequest({ url: href, userData: { kind: "product", candidateTitle: label } });
            queued++;
        }
        const next = document.querySelector('a[rel="next"], a[aria-label*="next" i], a[title*="next" i], a.next, .pagination a.next');
        if (next && next.href && sameRetailer(next.href)) {
            await context.enqueueRequest({ url: next.href, userData: { kind: "catalog" } });
        }
        log.info("Catalog " + retailer + ": queued " + queued + " Pokémon TCG product links");
        return null;
    }

    const og = document.querySelector('meta[property="og:title"]');
    const h1 = document.querySelector("h1");
    const title = ((og && og.content) || (h1 && h1.innerText) || document.title || data.candidateTitle || "")
        .replace(/\s+/g, " ").trim().slice(0, 250);
    const combined = (title + " " + pageText).slice(0, 32000);
    if (!tcg(title) && !tcg(data.candidateTitle)) return null;

    const priceNode = document.querySelector('meta[property="product:price:amount"], meta[property="og:price:amount"], meta[itemprop="price"]');
    let price = priceNode && (priceNode.content || priceNode.getAttribute("content"));
    if (!price) {
        const m = combined.match(/(?:price|our price|now)\s*:?\s*\$\s*([\d,]+(?:\.\d{2})?)/i) || combined.match(/\$\s*([\d,]+\.\d{2})/);
        if (m) price = m[1].replace(/,/g, "");
    }
    const sellers = [];
    for (const m of combined.matchAll(/sold\s*(?:and|&)\s*shipped\s*by\s+([a-z0-9 .&'-]{2,60})/ig)) sellers.push(m[1].trim());
    for (const m of combined.matchAll(/sold by\s+([a-z0-9 .&'-]{2,60})/ig)) sellers.push(m[1].trim());
    const expected = retailer.toLowerCase();
    const aliases = retailer === "Best Buy" ? ["best buy", "bestbuy"] : [expected];
    const sellerMatch = sellers.find(s => aliases.some(a => s.toLowerCase().includes(a)));
    const marketplace = /marketplace seller|third.party seller|sold by a third party|more options from marketplace/i.test(combined);
    const sellerVerified = sellerMatch ? true : (!strictSeller && !marketplace && sellers.length === 0);
    const seller = sellerMatch || (sellerVerified ? retailer + " (retailer-owned offer)" : (sellers[0] || "Seller not verified"));

    let availability = "unknown";
    if (/out of stock|sold out|currently unavailable|not available online|shipping unavailable/i.test(combined)) availability = "out_of_stock";
    else if (/in stock|add to cart|ship it|shipping available|available online|ready to ship/i.test(combined) &&
             !/out of stock|currently unavailable|not available online/i.test(combined)) availability = "in_stock";

    return {
        retailer, title: tcg(title) ? title : String(data.candidateTitle || title).slice(0, 250),
        url: request.url, price: price || null, availability,
        seller, seller_verified: sellerVerified, page_text: pageText.slice(0, 6000),
        page_status: "rendered"
    };
}
"""


def scan_retailer(retailer, catalog_url, seed_urls, domain):
    """Run a bounded browser crawl and return only credible first-party TCG records."""
    token = os.getenv("APIFY_API_TOKEN", "").strip()
    if not token:
        return []
    actor = os.getenv("APIFY_ACTOR_ID", "apify~web-scraper").strip()
    max_pages = max(10, min(int(os.getenv("APIFY_MAX_PAGES", "80")), 250))
    max_wait = max(120, min(int(os.getenv("APIFY_MAX_WAIT_SECONDS", "720")), 1800))
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    starts = []
    if catalog_url:
        starts.append({"url": catalog_url, "userData": {"kind": "catalog"}})
    for url in seed_urls or []:
        if url and urlparse(url).hostname and (urlparse(url).hostname == domain or urlparse(url).hostname.endswith("." + domain)):
            starts.append({"url": url, "userData": {"kind": "product"}})
    if not starts:
        return []

    payload = {
        "startUrls": starts,
        "pageFunction": PAGE_FUNCTION,
        "proxyConfiguration": {},
        "linkSelector": "",
        "maxPagesPerCrawl": max_pages,
        "maxResultsPerCrawl": 500,
        "maxConcurrency": 4,
        "maxRequestRetries": 1,
        "pageLoadTimeoutSecs": 40,
        "pageFunctionTimeoutSecs": 30,
        "waitUntil": ["domcontentloaded"],
        "maxScrollHeightPixels": 5000,
        "closeCookieModals": True,
        "customData": {"retailer": retailer, "domain": domain},
    }
    cap = os.getenv("APIFY_MAX_TOTAL_CHARGE_USD", "0.25").strip()
    query = {"waitForFinish": "60"}
    if cap:
        query["maxTotalChargeUsd"] = cap

    log.info("%s: starting Apify Web Scraper (%s max pages)", retailer, max_pages)
    response = requests.post(
        f"{API}/actors/{actor}/runs",
        params=query, headers=headers, json=payload, timeout=90
    )
    response.raise_for_status()
    run = response.json().get("data") or {}
    run_id = run.get("id")
    if not run_id:
        raise RuntimeError("Apify did not return an Actor run ID")
    status = run.get("status")
    deadline = time.monotonic() + max_wait
    while status not in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT") and time.monotonic() < deadline:
        time.sleep(5)
        poll = requests.get(
            f"{API}/actor-runs/{run_id}",
            params={"waitForFinish": "60"}, headers=headers, timeout=75
        )
        poll.raise_for_status()
        run = poll.json().get("data") or {}
        status = run.get("status")
    if status != "SUCCEEDED":
        raise RuntimeError(f"Apify Actor ended with status {status or 'unknown'}")
    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        return []
    result = requests.get(
        f"{API}/datasets/{dataset_id}/items",
        params={"clean": "true", "format": "json", "limit": "1000"},
        headers=headers, timeout=90
    )
    result.raise_for_status()
    items = result.json()
    output = []
    seen = set()
    for item in items if isinstance(items, list) else []:
        url = str(item.get("url") or "")
        title = str(item.get("title") or "").strip()
        if (item.get("retailer") != retailer or not item.get("seller_verified")
                or not _is_tcg_title(title) or not url or url in seen):
            continue
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host != domain and not host.endswith("." + domain):
            continue
        seen.add(url)
        output.append({
            "title": title[:250], "url": url,
            "price": item.get("price"),
            "availability": item.get("availability", "unknown"),
            "seller": item.get("seller") or retailer,
            "seller_verified": True,
            "page_text": item.get("page_text", ""),
        })
    log.info("%s: Apify returned %s verified TCG records from %s dataset items", retailer, len(output), len(items) if isinstance(items, list) else 0)
    return output
