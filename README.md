# Better Days — Find Your Perfect SF Neighborhood

Scores 117 San Francisco neighborhoods from 200k 311 service requests.
**Postgres (ClickHouse Cloud managed Postgres)** is the system of record: raw 311 cases, neighborhood polygons, user
preferences, saved neighborhoods, chat log. **ClickHouse** ingests straight from Postgres (`postgresql()` table function)
and does the analytics (rollups, quantiles, geohash heat grid, polygon dictionary). An LLM (OpenAI, Anthropic fallback)
answers questions with tools over the same API. Built for the ClickHouse *Better Days* hackathon (2026-08-28).

## Run
```bash
cp .env.example .env            # fill in ClickHouse Cloud / Postgres / Anthropic creds (local CH works with defaults)
make data                       # 311_Cases.csv (116 MB) -> data/cases.csv.gz (12 MB, 16 columns, ISO dates)
make load                       # ClickHouse schema + MV + polygon dictionary, then ingest FROM POSTGRES (~70 s)
                                #   (add --from-csv to load the local files instead, ~1 s)
make api                        # FastAPI + the map UI on http://localhost:8800  (docs: /docs, spec: /openapi.json)
.venv/bin/python scripts/smoke.py
```
Local dev ClickHouse: `cd services/clickhouse && ./clickhouse server --config-file=config.xml` (ports 8123/9000).
Switch to ClickHouse Cloud by setting `CLICKHOUSE_HOST`, `CLICKHOUSE_PASSWORD`, `CLICKHOUSE_PORT=8443`, `CLICKHOUSE_SECURE=1` — same loader, same code.

## How scoring works
| Dimension | Source categories | Metric |
|---|---|---|
| cleanliness | Street & Sidewalk Cleaning, Litter Receptacles, Illegal Postings | cases / km² / year |
| graffiti | Graffiti* | cases / km² / year |
| street_safety | Encampments, Homeless Concerns, Blocked Sidewalk | cases / km² / year |
| quiet | Noise Report | cases / km² / year |
| parking | Parking Enforcement, Abandoned Vehicle, Color Curb | cases / km² / year |
| infrastructure | Streetlights, Street Defects, Sidewalk/Curb, Sewer, Trees, Damaged Property | cases / km² / year |
| responsiveness | all closed cases | median of (closure time ÷ city median for the same request type) |

Rates are shrunk toward the city median in proportion to how little exposure (km²·years) a neighborhood has
(empirical Bayes, `SHRINK_K`), so 30-case neighborhoods don't top the list on luck. Each metric is then
percentile-ranked across neighborhoods → 0–100 (100 = best). A user's weights (0–5) give a weighted
score. Window defaults to 2018-01 → 2024-12 (the sample's dense years). Area comes from the polygon
(`polygonAreaSpherical`), not population — so big low-density areas (Lakeshore, Hunters Point) score well; treat as a proxy.

## Frontend (`frontend/index.html`, served at `/`)
One static page, no build step: MapLibre GL (vendored) on a CARTO dark basemap.
- **Neighborhood score** choropleth (vivid blue = better) with the top-8 labelled; **Complaint heat** layer per dimension (geohash-7 cells from ClickHouse).
- Weight sliders + presets (Balanced / Family / Quiet & tidy / City energy / Car owner) re-rank all 117 neighborhoods instantly client-side; "Stay near" filters by radius from any neighborhood.
- Click a polygon or ranking row → drilldown drawer: score with your weights, per-dimension bars, cases/km²/yr, median close time and closure-vs-city, top request types, cases by year, **Save to shortlist** (Postgres), **Compare with…** (side-by-side bars).
- Chat dock → `/api/chat`: the model calls `recommend` / `neighborhood` / `compare`, the answer sets the sliders, highlights the picks on the map and opens the drawer.
- Degrades without WebGL (ranking, drawer, chat still work).

## Mode 2 — Find a place through your network (`backend/network.py`, `scripts/seed_network.py`)
Synthetic demo network (41 people, 113 edges, 12 listings) seeded into Postgres; `make seed-network` to rebuild.
1. **Profile** — LinkedIn/X handles, budget, room type, must-haves (Postgres `user_profile`).
2. **Network** — 1st degree = your edges, 2nd = edges of members you're linked to (BFS). Badges: ✓ verified = edge on both sides, ✓ In SF = X location resolves to SF (regex; real X lookup is a one-function swap), 🏠 has a place.
3. **Listings pool** — members' own listings, listings *shared by* your connections (owner off-platform), and "found on the web" URL imports.
4. **Fit score** — 0.45 × neighborhood score (your mode-1 weights, ClickHouse) + 0.40 × price vs fair range (`rent_benchmarks`, *illustrative*) & budget + 0.15 × must-haves. "Perfect match" = neighborhood ≥ 60, rent ≤ min(budget, fair median +5%), all must-haves.
5. **Reach** — member → *Chat now*; shared by a connection → *Ask <bridge> for an intro*; off-platform → *Invite owner by email* (Resend if `RESEND_API_KEY`, else on-screen outbox) with a magic join link (`/?join=TOKEN`) that makes them a member and unlocks chat.
6. **Negotiate** — chat with the owner (for the demo an AI persona with a hidden reservation price, `OPENAI_FAST_MODEL`). After every turn the **Advisor** computes fit, fair range, suggested counter and a verdict: ACCEPT / COUNTER $X / **NEGOTIATE VIA <connection> — or walk away** / WALK AWAY, shown as a price ladder (their price · offer this · budget · never above). "Ask Priya to negotiate" (`POST /negotiation/{id}/via`) has the bridge vouch for you; the owner's floor drops ~6% and they come back with a softer price.
7. **Funnel** — every step is an event in ClickHouse `better_days.events` → invites → joined → chats → offers → deals.

Endpoints: `/api/net/graph/{user}`, `/api/net/listings/{user}`, `/api/net/profile`, `/api/net/invite`, `/api/net/join/{token}`, `/api/net/outbox/{user}`, `/api/net/negotiation/{listing}` (+`/message`), `/api/net/funnel/{user}`.

## API (`backend/main.py`)
| Endpoint | Purpose |
|---|---|
| `GET /api/neighborhoods.geojson` | 117 polygons + all scores/rates (choropleth; weights applied client-side) |
| `GET /api/heatmap?dimension=&precision=5..8` | geohash cells `[lat, lon, count]` |
| `POST /api/recommend` | weights + optional `near`/`max_km` → ranked neighborhoods with reasons |
| `GET /api/neighborhood/{name}` | drilldown: scores, top request types, closure times, yearly trend |
| `GET /api/compare?a=&b=` | side-by-side |
| `GET /api/trends?dimension=` | monthly city-wide counts |
| `POST /api/chat` | LLM (OpenAI Responses API, `OPENAI_MODEL`; Anthropic if only that key is set) with tools `recommend` / `neighborhood` / `compare`; returns `highlight` for the map; logs to Postgres `chat_log` |
| `POST/GET /api/prefs`, `/api/saved` | Postgres (needs `DATABASE_URL`) |

`/openapi.json` (with `PUBLIC_URL` set to an ngrok URL) is what a LibreChat Agent Action consumes.

## ClickHouse features used (`sql/`)
`MATERIALIZED` columns (`resolution_hours`, `geohash6`, `month`), `AggregatingMergeTree` rollup fed by a
materialized view (`countState`, `quantilesState`), a **polygon dictionary** (`LAYOUT(POLYGON)`) to assign
neighborhoods to un-tagged points via `dictGet`, `readWKTMultiPolygon` + `polygonAreaSpherical`,
`geohashEncode` for the heat grid, `LowCardinality` everywhere, and the **`postgresql()` table function** to ingest
directly from the Postgres system of record (Postgres tables: `cases311`, `neighborhoods`, `prefs`, `saved_neighborhoods`, `chat_log`).

## .env layout
```
CLICKHOUSE_HOST / PORT / USER / PASSWORD / SECURE   # a ClickHouse *service* (Cloud: port 8443, SECURE=1) or localhost:8123
PGHOST / PGPORT / PGUSER / PGPASSWORD / PGDATABASE  # ClickHouse Cloud managed Postgres (or DATABASE_URL)
OPENAI_API_KEY / OPENAI_MODEL                       # e.g. gpt-5.6-luna
PUBLIC_URL                                          # ngrok URL for LibreChat Actions (optional)
```
