# Better Days — "Find Your Perfect SF Neighborhood"
ClickHouse *Better Days* hackathon, SF, 2026-08-28. Open track. Submissions 5:00 PM, demos 5:05 PM.
Prizes: Grand $1k + $500 CH credits · Runner-up $500 + $300 · **LibreChat bonus $250**.
Required: ClickHouse (OLAP) + PostgreSQL (OLTP). No Docker on this machine.

## 1. What the data actually is
| File | Rows | What it's good for | Gotchas |
|---|---|---|---|
| `311_Cases.csv` (116 MB) | 215,367 | The whole product. 107 categories, lat/lon on 99.98%, opened/closed timestamps, neighborhood + district pre-tagged | 50 cols, ~30 are `DELETE - …` junk → keep 16. Years are lumpy (2018–22 + 2024 heavy; 2025–26 only 486 rows) so "right now" = last 2–3 yrs, not last month. 6.7k rows have blank neighborhood → fill with `pointInPolygon` |
| `Neighborhoods_from_6ia5_2f8k.csv` | 117 polygons (WKT) | Choropleth + "which neighborhood" join. 311's `Neighborhood` column matches these names for 94% of rows | Includes parks/islands (Presidio, Treasure Island, McLaren Park) — filter or flag "not residential" |
| `Current_Supervisor_Districts_2.csv` | 11 polygons | Optional district roll-up | Stale (2018 supervisors). 311 already carries district number. **Skip unless time is left.** |

Category → livability dimension mapping (this is the core modelling decision):
- **Cleanliness** — Street and Sidewalk Cleaning (88k), Litter Receptacles, Illegal Postings
- **Graffiti** — Graffiti, Graffiti Public
- **Street safety / homelessness** — Encampments, Encampment, Homeless Concerns, Blocked Street or SideWalk
- **Quiet** — Noise Report
- **Parking & vehicles** — Parking Enforcement, Abandoned Vehicle, Color Curb
- **Infrastructure** — Streetlights, Street Defects, Sidewalk or Curb, Sewer Issues, Tree Maintenance, Damaged Property
- **City responsiveness** — median `closed - opened` hours (p50 overall 21.5h; cleaning 13h, graffiti 115h, trees 242h)

Metric per neighborhood per dimension: **cases / km² / year** (area from the WKT polygon — no population data in these files), then percentile-rank across the 117 neighborhoods. User picks weights → weighted score → ranking. That is the "perfect neighborhood".

## 2. Stack (decided 12:45): everything hosted, only Python + LibreChat run locally
| Layer | Choice | Notes |
|---|---|---|
| OLAP | **ClickHouse Cloud** (HTTPS :8443) | cases table, MV rollups per nbhd×dimension×month, `geohashEncode` heat grid, `pointInPolygon(readWKTMultiPolygon)` for blank neighborhoods, `quantiles()` response time. Load via trimmed gzip CSV (~15 MB) **or** `url()` straight from data.sfgov.org (no upload; can use full dataset) |
| OLTP | **Hosted Postgres** (Neon/Supabase — no local brew PG) | `users`, `preferences` (weights), `saved_neighborhoods`, `chat_recommendations`. Stretch: ClickPipes Postgres CDC → ClickHouse Cloud so user activity is queryable next to 311 data ("PB&J" demo) |
| API | Python **FastAPI :8800** — `clickhouse-connect` + `psycopg` | `/heatmap`, `/neighborhoods.geojson`, `/recommend`, `/neighborhood/{name}`, `/prefs`. OpenAPI spec = LibreChat Action |
| Map UI | Static HTML + MapLibre GL (no token) | heatmap + choropleth + weight sliders + click drilldown + **built-in chatbox** (Claude tool-use via `/chat` endpoint; tools = `recommend`, `neighborhood`, `compare`; answers highlight neighborhoods on the map) |
| LibreChat (bonus) | **Hosted instance — Railway one-click template** (brings its own Mongo; nothing on the laptop) or the sponsor's instance if they have one. Integration = **Agent → Action → paste our OpenAPI URL** through a `cloudflared` quick tunnel to :8800. No librechat.yaml, no MCP, no Mongo we touch | LLM key set as Railway env var |

Local ClickHouse binary (`services/clickhouse/`) and brew Postgres are now **unused** — stop the Trash-resident CH process (pid 40058 + watchdog 40055) and `brew services stop postgresql@16` when convenient.

## 3. Needed before build starts
1. ClickHouse Cloud: host, user, password (and confirm `url()` egress is allowed if we pull from sfgov).
2. Postgres connection string (hosted).
3. LLM API key for LibreChat (goes in Railway env). Railway account (free trial credits) — or ask the LibreChat table for a hosted instance.
4. Confirm data window: 2018-01 → 2024-12 from the 215k sample, or full dataset via `url()`.

## 4. Build order / time budget (now ≈ 12:35, submit 17:00)
- **12:35–13:05 Ingest** — restart CH from project dir; `CREATE` tables; `INSERT … FROM file()` with just the 16 columns; MV rollups; area + percentile scoring query. Postgres `prefs` table.
- **13:05–14:00 Backend** — FastAPI, 5 endpoints, CORS on, OpenAPI json (needed for LibreChat Actions).
- **14:00–15:30 Frontend** — map + heatmap + choropleth + sliders + drilldown + chatbox (`/chat` proxies to Claude with the 3 tools; same functions LibreChat calls).
- **15:30–16:30 LibreChat** (parallel) — deploy Railway template (~5 min) → `cloudflared tunnel --url http://localhost:8800` → Agent + Action from `/openapi.json` (3 ops: `recommend`, `neighborhood`, `compare`; `x-strict: true`) → system prompt "always end with the map link".
- **16:30–17:00 Demo** — 3 canned questions, 1 slide on the CH+PG split.

Divide across team: A = data+backend, B = frontend, C = Railway LibreChat + tunnel + Agent prompt (can start once `/openapi.json` exists, ~14:00).
