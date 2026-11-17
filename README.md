# PokeTrack

A Flask API for discovering Pokémon TCG product listings on retailer websites using public search result pages. No paid search API key is required.

## Deploy on Railway

1. Create a Railway service from this GitHub repository.
2. Add a persistent volume mounted at `/data` so SQLite data survives restarts.
3. Set `DB_PATH=/data/poketrack.db`, `START_ZIP=22601`, and `RADIUS_MILES=200`.
4. Optionally set `SCAN_TOKEN` and send it as the `X-Scan-Token` header to protect scan endpoints.
5. Add a Railway Cron service using the same repo and run `curl -X POST https://YOUR-APP-URL/api/scan` on your desired schedule.

## API

- `GET /health`
- `POST /api/scan` — scans all configured retailers
- `POST /api/scan-retailer?name=Target` — scans one retailer
- `GET /api/products` — lists discovered products; filter with `retailer` and `availability`
- `GET /api/retailer-status`
- `GET /api/history`

Configured retailers: Target, Walmart, Best Buy, Amazon, Barnes & Noble, Costco, Sam's Club, Walgreens, Dollar General, Hobby Lobby, Books-A-Million, and Gabe's.

## Important limitations

This first version discovers online product pages, not verified shelf-level inventory at individual store branches. A retailer is marked `in_stock` or `out_of_stock` only when its product page or search snippet contains explicit availability wording; otherwise it remains `unknown`. Search engines and retailers may block automated requests or change page markup, so scan results are best-effort discovery, not guaranteed live inventory. No stock counts are fabricated.
