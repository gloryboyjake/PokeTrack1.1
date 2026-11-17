# PokeTrack Radar

A single Flask application and dashboard for discovering Pokémon TCG product listings and physical retailer locations around Winchester, Virginia.

## Defaults
- Starting ZIP: 22601
- Radius: 80 miles (straight-line distance from the ZIP reference point)
- Physical store directory: researched retailer branch locations in `store_directory.json`
- Store refresh: ZIP-centroid geocoding and Haversine distance filtering
- ZIP geocoding: Zippopotam.us with US Census fallback for the origin ZIP
- Product discovery: public retailer search pages; no paid search API key
- Map services: none

## Store directory
The directory is grouped alphabetically by retailer brand and contains individual physical branch addresses and retailer verification links. The refresh job geocodes each branch ZIP, calculates straight-line distance from ZIP 22601, and only saves branches within the configured radius. No OpenStreetMap, Overpass, or map API is used.

The current researched directory includes Target, Walmart, Best Buy, Costco, Hobby Lobby, Dollar General, Walgreens, Barnes & Noble, and Sam's Club, plus the existing Books-A-Million and Gabe's entries. Store locator listings can change; refreshes retain the source link for verification.

## Deploy
1. Deploy this repository's `rebuild/clean-start` branch to Railway.
2. Attach a persistent volume mounted at `/data`.
3. Set `DB_PATH=/data/poketrack.db`, `START_ZIP=22601`, and `RADIUS_MILES=80`.
4. Start with `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 900`.
5. Set the health check path to `/health`.
6. Configure a Railway cron service to POST to `/api/scan` every five minutes. The cron must call this same app and database.

## Dashboard and API
- `/` — responsive PokeTrack dashboard
- `/health` — health check
- `GET /api/stores` — physical store locations within the configured radius
- `GET /api/products` — discovered online product listings
- `GET /api/history` — scan history
- `GET /api/summary` — dashboard summary
- `POST /api/scan` — full scan (store directory and product listings)
- `POST /api/scan?kind=stores` — refresh store directory
- `POST /api/scan?kind=products` — refresh product listings

## Accuracy limits
The branch directory is manually researched from retailer store locators and is only as current as those sources. ZIP-centroid coordinates are used for distance estimates, so distance is approximate rather than an address-level route distance. Product discovery is best-effort and depends on public search and retailer page access. A product page is not proof of local branch stock. Availability is marked in stock or out of stock only when explicit wording appears on the retailer page; otherwise it remains unknown. No inventory counts are fabricated.

## Data
SQLite is stored at `/data/poketrack.db`. Keep the Railway volume attached at `/data` when redeploying. The app creates its tables if missing and does not drop existing tables.
