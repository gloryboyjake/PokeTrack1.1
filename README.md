# PokeTrack Radar

A single Flask application and dashboard for discovering Pokémon TCG product listings and physical retailer locations around Winchester, Virginia.

## Defaults
- Starting ZIP: 22601
- Radius: 200 miles (straight-line distance)
- Scan cadence: intended every 5 minutes through a Railway cron service
- Store directory: OpenStreetMap / Overpass
- ZIP geocoding: US Census Geocoder
- Product discovery: public retailer search pages; no paid search API key

## Deploy
1. Deploy this repository's `rebuild/clean-start` branch to Railway.
2. Attach a persistent volume mounted at `/data`.
3. Set `DB_PATH=/data/poketrack.db`, `START_ZIP=22601`, and `RADIUS_MILES=200`.
4. Start with `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 900`.
5. Set the health check path to `/health`.
6. Configure a Railway cron service to POST to `/api/scan` every five minutes. The cron must call this same app and database.

## Dashboard and API
- `/` — responsive PokeTrack dashboard
- `/health` — health check
- `GET /api/stores` — discovered physical store locations
- `GET /api/products` — discovered online product listings
- `GET /api/history` — scan history
- `GET /api/summary` — dashboard summary
- `POST /api/scan` — full scan (store directory and product listings)
- `POST /api/scan?kind=stores` — refresh store directory
- `POST /api/scan?kind=products` — refresh product listings

## Important accuracy limits
The store directory is based on OpenStreetMap tags and coverage; it can miss branches or contain incomplete addresses. The radius is calculated from the geocoded ZIP reference point using straight-line distance. Product discovery is best-effort and depends on public search and retailer page access. A product page is not proof of local branch stock. Availability is marked in stock or out of stock only when explicit wording appears on the retailer page; otherwise it remains unknown. No inventory counts are fabricated.

## Data
SQLite is stored at `/data/poketrack.db`. Keep the Railway volume attached at `/data` when redeploying. The app creates its tables if missing and does not drop existing tables.
