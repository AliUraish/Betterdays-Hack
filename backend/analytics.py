"""ClickHouse queries + neighborhood scoring for Better Days.

ClickHouse does the heavy lifting (rollups via MV, quantiles, geohash grid, polygon dictionary);
Python only ranks 117 neighborhoods and formats.
"""
import math, os, time
from functools import lru_cache
from pathlib import Path
from dotenv import load_dotenv
import clickhouse_connect

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DIMENSIONS = {
    "cleanliness":    {"label": "Cleanliness",        "desc": "street & sidewalk cleaning, litter, illegal postings"},
    "graffiti":       {"label": "Graffiti",           "desc": "graffiti reports"},
    "street_safety":  {"label": "Street safety",      "desc": "encampments, homeless concerns, blocked sidewalks"},
    "quiet":          {"label": "Quiet",              "desc": "noise reports"},
    "parking":        {"label": "Parking",            "desc": "parking enforcement, abandoned vehicles"},
    "infrastructure": {"label": "Infrastructure",     "desc": "streetlights, street defects, sidewalks, sewer, trees"},
    "responsiveness": {"label": "City responsiveness","desc": "median hours for the city to close a case"},
}
RATE_DIMS = [d for d in DIMENSIONS if d != "responsiveness"]
DEFAULT_WEIGHTS = {"cleanliness": 3, "graffiti": 2, "street_safety": 3, "quiet": 3,
                   "parking": 2, "infrastructure": 2, "responsiveness": 2}
DEFAULT_FROM, DEFAULT_TO = "2018-01-01", "2024-12-31"

_client = None
def ch():
    global _client
    if _client is None:
        _client = clickhouse_connect.get_client(
            host=os.getenv("CLICKHOUSE_HOST", "localhost").replace("https://", "").replace("http://", "").strip("/"),
            port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
            username=os.getenv("CLICKHOUSE_USER", "default"),
            password=os.getenv("CLICKHOUSE_PASSWORD", ""),
            secure=os.getenv("CLICKHOUSE_SECURE", "0") == "1",
            autogenerate_session_id=False,   # stateless queries -> safe across FastAPI worker threads
            connect_timeout=20, send_receive_timeout=120,
        )
    return _client

def _years(f, t):
    y0, m0 = int(f[:4]), int(f[5:7]); y1, m1 = int(t[:4]), int(t[5:7])
    return max(((y1 - y0) * 12 + (m1 - m0) + 1) / 12.0, 1 / 12.0)

def _to_list(x):
    return [_to_list(i) for i in x] if isinstance(x, (list, tuple)) else x

# ---------------------------------------------------------------- neighborhoods (static)
@lru_cache(maxsize=1)
def neighborhoods():
    rows = ch().query("SELECT id, name, link, residential, centroid_lat, centroid_lon, area_km2, poly "
                      "FROM better_days.neighborhoods ORDER BY name").result_rows
    out = {}
    for id_, name, link, res, clat, clon, area, poly in rows:
        out[name] = {"id": id_, "name": name, "link": link, "residential": bool(res),
                     "centroid": [clat, clon], "area_km2": area, "geometry": _to_list(poly)}
    return out

def resolve_name(q):
    """Case-insensitive exact, then substring match on neighborhood names."""
    if not q: return None
    names = list(neighborhoods()); ql = q.strip().lower()
    for n in names:
        if n.lower() == ql: return n
    hits = [n for n in names if ql in n.lower() or n.lower() in ql]
    return hits[0] if hits else None

# ---------------------------------------------------------------- scores (cached per window)
_score_cache = {}
def scores(f=DEFAULT_FROM, t=DEFAULT_TO):
    key = (f, t)
    if key in _score_cache and time.time() - _score_cache[key]["ts"] < 600:
        return _score_cache[key]["data"]
    yrs = _years(f, t)
    params = {"f": f, "t": t}
    rows = ch().query("""
        SELECT neighborhood, dimension, countMerge(n) AS n, countMerge(n_closed) AS n_closed,
               quantilesMerge(0.5, 0.9)(res_hours_q) AS q
        FROM better_days.nbhd_dim_month
        WHERE month >= toDate({f:String}) AND month <= toDate({t:String}) AND neighborhood != ''
        GROUP BY neighborhood, dimension""", parameters=params).result_rows
    # Responsiveness relative to category: each case's closure time divided by the city-wide median for its request
    # type, so a neighborhood full of slow-by-nature tree cases isn't punished for the mix. ratio 1.0 = city typical.
    resp = ch().query("""
        WITH cat AS (
            SELECT request_type, quantile(0.5)(resolution_hours) AS med FROM better_days.cases
            WHERE opened >= toDateTime({f:String}) AND opened <= toDateTime(concat({t:String}, ' 23:59:59'))
              AND resolution_hours IS NOT NULL GROUP BY request_type HAVING med > 0)
        SELECT c.neighborhood, quantiles(0.5, 0.9)(c.resolution_hours) AS q, count() AS n,
               quantile(0.5)(c.resolution_hours / cat.med) AS rel
        FROM better_days.cases c INNER JOIN cat ON cat.request_type = c.request_type
        WHERE c.opened >= toDateTime({f:String}) AND c.opened <= toDateTime(concat({t:String}, ' 23:59:59'))
          AND c.resolution_hours IS NOT NULL AND c.neighborhood != ''
        GROUP BY c.neighborhood""", parameters=params).result_rows

    nb = neighborhoods()
    data = {n: {"name": n, "residential": v["residential"], "area_km2": v["area_km2"], "centroid": v["centroid"],
                "total_cases": 0, "counts": {d: 0 for d in RATE_DIMS}, "rates": {}, "scores": {},
                "p50_hours": None, "p90_hours": None, "resp_rel": None, "n_closed": 0} for n, v in nb.items()}
    for n, d, cnt, ncl, q in rows:
        if n not in data: continue
        data[n]["total_cases"] += cnt
        if d in RATE_DIMS: data[n]["counts"][d] = cnt
    for n, q, cnt, rel in resp:
        if n in data:
            data[n]["p50_hours"], data[n]["p90_hours"], data[n]["resp_rel"], data[n]["n_closed"] = round(q[0], 1), round(q[1], 1), round(rel, 2), cnt

    def pct_scores(values):  # values: {name: metric}, lower is better -> score 100..0
        items = sorted(values.items(), key=lambda kv: kv[1]); N = len(items)
        return {n: round(100 * (1 - i / max(N - 1, 1)), 1) for i, (n, _) in enumerate(items)}

    # Empirical-Bayes shrinkage: a neighborhood's rate is pulled toward the city median in proportion to how little
    # evidence it has. K = "prior weight" in km²·years; a small area with a year or two of data leans on the prior,
    # a big busy one barely moves. Stops 30-case neighborhoods topping the ranking on luck.
    K = float(os.getenv("SHRINK_K", "3.0"))
    medians = {}
    for d in RATE_DIMS:
        raw = {n: v["counts"][d] / max(v["area_km2"], 0.01) / yrs for n, v in data.items()}
        srt = sorted(raw.values()); med = srt[len(srt) // 2]; medians[d] = round(med, 2)
        rates = {}
        for n, v in data.items():
            exposure = max(v["area_km2"], 0.01) * yrs                      # km²·years of observation
            rates[n] = (v["counts"][d] + K * med) / (exposure + K)          # shrunk rate
            data[n]["rates"][d] = round(raw[n], 2)                          # show the raw rate to users
            data[n].setdefault("rates_adj", {})[d] = round(rates[n], 2)
        for n, s in pct_scores(rates).items(): data[n]["scores"][d] = s
    KR = float(os.getenv("SHRINK_K_RESP", "40"))                          # prior weight in closed cases
    resp_vals = {n: (v["resp_rel"] * v["n_closed"] + 1.0 * KR) / (v["n_closed"] + KR)
                 for n, v in data.items() if v["resp_rel"] is not None}
    for n, s in pct_scores(resp_vals).items(): data[n]["scores"]["responsiveness"] = s
    for n, v in data.items(): v["scores"].setdefault("responsiveness", 50.0)
    srt = sorted(resp_vals.values()); medians["responsiveness"] = srt[len(srt) // 2] if srt else None

    result = {"window": {"from": f, "to": t, "years": round(yrs, 2)}, "city_median": medians, "neighborhoods": data}
    _score_cache[key] = {"ts": time.time(), "data": result}
    return result

def weighted(sc, weights):
    w = {d: float(weights.get(d, 0)) for d in DIMENSIONS}
    tot = sum(w.values()) or 1.0
    return round(sum(w[d] * sc.get(d, 50) for d in DIMENSIONS) / tot, 1)

def haversine_km(a, b):
    R = 6371.0; la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))

def _reasons(v, medians):
    sc = v["scores"]; out = []
    for d, s in sorted(sc.items(), key=lambda kv: -kv[1])[:2]:
        if s >= 60: out.append(_reason_line(d, v, medians, good=True))
    for d, s in sorted(sc.items(), key=lambda kv: kv[1])[:1]:
        if s < 45: out.append(_reason_line(d, v, medians, good=False))
    return out

def _reason_line(d, v, medians, good):
    lab = DIMENSIONS[d]["label"]; s = v["scores"][d]
    if d == "responsiveness":
        return f"{lab} {s:.0f}/100: cases closed {v['resp_rel']}x the city-typical time for the same request type (median {v['p50_hours']}h)"
    return f"{lab} {s:.0f}/100: {v['rates'][d]} cases/km²/yr vs city median {medians[d]}"

def recommend(weights=None, near=None, top_n=5, max_km=3.0, residential_only=True, f=DEFAULT_FROM, t=DEFAULT_TO):
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    S = scores(f, t); nb = neighborhoods()
    anchor = None; near_name = None
    if near:
        if "," in near and near.replace(",", "").replace(".", "").replace("-", "").replace(" ", "").isdigit():
            anchor = [float(x) for x in near.split(",")]
        else:
            near_name = resolve_name(near)
            if near_name: anchor = nb[near_name]["centroid"]
    ranked = []
    for n, v in S["neighborhoods"].items():
        if residential_only and not v["residential"]: continue
        if v["total_cases"] < 30: continue
        item = {"name": n, "score": weighted(v["scores"], weights), "scores": v["scores"],
                "total_cases": v["total_cases"], "p50_hours": v["p50_hours"], "closure_time_vs_city": v["resp_rel"], "centroid": v["centroid"],
                "reasons": _reasons(v, S["city_median"])}
        if anchor:
            item["distance_km"] = round(haversine_km(anchor, v["centroid"]), 2)
            if item["distance_km"] > max_km: continue
        ranked.append(item)
    ranked.sort(key=lambda x: -x["score"])
    for i, r in enumerate(ranked): r["rank"] = i + 1
    return {"weights": weights, "near": near_name or near, "anchor": anchor, "max_km": max_km if anchor else None,
            "window": S["window"], "candidates": len(ranked), "results": ranked[:top_n]}

def neighborhood_detail(name, f=DEFAULT_FROM, t=DEFAULT_TO):
    n = resolve_name(name)
    if not n: return None
    S = scores(f, t); v = S["neighborhoods"][n]; nb = neighborhoods()[n]
    params = {"n": n, "f": f, "t": t}
    top_types = ch().query("""
        SELECT category, request_type, count() c FROM better_days.cases
        WHERE neighborhood = {n:String} AND opened >= toDateTime({f:String}) AND opened <= toDateTime(concat({t:String},' 23:59:59'))
        GROUP BY 1,2 ORDER BY c DESC LIMIT 8""", parameters=params).result_rows
    trend = ch().query("""
        SELECT toYear(month) y, countMerge(n) c FROM better_days.nbhd_dim_month
        WHERE neighborhood = {n:String} AND month >= toDate({f:String}) AND month <= toDate({t:String})
        GROUP BY y ORDER BY y""", parameters=params).result_rows
    all_ranked = recommend(top_n=200, residential_only=False, f=f, t=t)["results"]
    rank = next((r["rank"] for r in all_ranked if r["name"] == n), None)
    return {"name": n, "link": nb["link"], "residential": v["residential"], "area_km2": round(v["area_km2"], 2),
            "centroid": v["centroid"], "window": S["window"], "total_cases": v["total_cases"],
            "cases_per_km2_yr": round(v["total_cases"] / max(v["area_km2"], 0.01) / S["window"]["years"], 1),
            "scores": v["scores"], "rates": v["rates"], "counts": v["counts"], "city_median": S["city_median"],
            "p50_hours": v["p50_hours"], "p90_hours": v["p90_hours"], "closure_time_vs_city": v["resp_rel"],
            "overall_score_default_weights": weighted(v["scores"], DEFAULT_WEIGHTS),
            "overall_rank_default_weights": rank, "of": len(all_ranked),
            "top_request_types": [{"category": c, "request_type": r, "count": k} for c, r, k in top_types],
            "cases_by_year": [{"year": int(y), "count": c} for y, c in trend],
            "reasons": _reasons(v, S["city_median"])}

def compare(a, b, f=DEFAULT_FROM, t=DEFAULT_TO):
    da, db = neighborhood_detail(a, f, t), neighborhood_detail(b, f, t)
    if not da or not db: return None
    deltas = {d: round(da["scores"][d] - db["scores"][d], 1) for d in DIMENSIONS}
    return {"a": da, "b": db, "score_delta_a_minus_b": deltas,
            "better_a": [d for d, x in deltas.items() if x > 5], "better_b": [d for d, x in deltas.items() if x < -5]}

def heatmap(dimension="all", precision=7, f=DEFAULT_FROM, t=DEFAULT_TO):
    precision = max(5, min(8, int(precision)))
    where = "opened >= toDateTime({f:String}) AND opened <= toDateTime(concat({t:String},' 23:59:59'))"
    params = {"f": f, "t": t, "p": precision}
    if dimension and dimension != "all":
        where += " AND dimension = {d:String}"; params["d"] = dimension
    rows = ch().query(f"""
        SELECT geohashEncode(lon, lat, {{p:UInt8}}) g, round(avg(lat),5) la, round(avg(lon),5) lo, count() c
        FROM better_days.cases WHERE {where} GROUP BY g""", parameters=params).result_rows
    return {"dimension": dimension, "precision": precision, "cells": [[la, lo, c] for _, la, lo, c in rows],
            "max": max((r[3] for r in rows), default=0)}

def trends(dimension="all", f=DEFAULT_FROM, t=DEFAULT_TO):
    where = "month >= toDate({f:String}) AND month <= toDate({t:String})"; params = {"f": f, "t": t}
    if dimension and dimension != "all":
        where += " AND dimension = {d:String}"; params["d"] = dimension
    rows = ch().query(f"SELECT month, countMerge(n) c FROM better_days.nbhd_dim_month WHERE {where} GROUP BY month ORDER BY month",
                      parameters=params).result_rows
    return [{"month": m.strftime("%Y-%m"), "count": c} for m, c in rows]

def health():
    c = ch()
    return {"clickhouse": c.command("SELECT version()"), "host": os.getenv("CLICKHOUSE_HOST", "localhost"),
            "cases": c.command("SELECT count() FROM better_days.cases"),
            "neighborhoods": c.command("SELECT count() FROM better_days.neighborhoods"),
            "rollup_rows": c.command("SELECT count() FROM better_days.nbhd_dim_month")}
