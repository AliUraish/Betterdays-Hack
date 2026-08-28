"""Smoke-test the API. Usage: .venv/bin/python scripts/smoke.py [base_url]"""
import json, sys, time, urllib.request, urllib.error
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8800"
def call(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as r: return r.status, json.loads(r.read()), time.time() - t
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}"), time.time() - t

s, d, t = call("/api/health"); print(f"health {s} {t:.2f}s", d)
s, d, t = call("/api/recommend", {}); print(f"\nrecommend defaults {s} {t:.2f}s candidates={d['candidates']}")
for r in d["results"]: print(f"  #{r['rank']} {r['name']:28s} {r['score']:5.1f} cases={r['total_cases']:5d} p50={r['p50_hours']}h | {r['reasons'][0] if r['reasons'] else ''}")
s, d, t = call("/api/recommend", {"weights": {"quiet": 5, "cleanliness": 5, "graffiti": 1, "street_safety": 3, "parking": 0, "infrastructure": 1, "responsiveness": 3},
                                  "near": "golden gate park", "max_km": 2.5, "top_n": 4})
print(f"\nrecommend near GGP {s} {t:.2f}s near={d['near']} candidates={d['candidates']}")
for r in d["results"]: print(f"  #{r['rank']} {r['name']:28s} {r['score']:5.1f} {r['distance_km']}km scores={ {k[:5]: int(v) for k, v in r['scores'].items()} }")
s, d, t = call("/api/neighborhood/mission"); print(f"\nneighborhood/mission {s} {t:.2f}s -> {d['name']} rank {d['overall_rank_default_weights']}/{d['of']} score {d['overall_score_default_weights']} cases/km2/yr {d['cases_per_km2_yr']} p50 {d['p50_hours']}h")
print("  top types:", [(x["request_type"], x["count"]) for x in d["top_request_types"][:4]]); print("  by year:", [(y["year"], y["count"]) for y in d["cases_by_year"]]); print("  reasons:", d["reasons"])
s, d, t = call("/api/compare?a=noe%20valley&b=tenderloin"); print(f"\ncompare {s} {t:.2f}s {d['a']['name']} {d['a']['overall_score_default_weights']} vs {d['b']['name']} {d['b']['overall_score_default_weights']} better_a={d['better_a']}")
s, d, t = call("/api/heatmap?dimension=cleanliness&precision=7"); print(f"\nheatmap {s} {t:.2f}s cells={len(d['cells'])} max={d['max']} sample={d['cells'][:2]}")
s, d, t = call("/api/neighborhoods.geojson"); print(f"geojson {s} {t:.2f}s features={len(d['features'])} bytes~{len(json.dumps(d))//1024}KB window={d['window']}")
s, d, t = call("/api/trends"); print(f"trends {s} {t:.2f}s months={len(d)} first={d[0]} last={d[-1]}")
s, d, t = call("/openapi.json"); print(f"openapi {s} servers={d['servers']} ops={sorted(op['operationId'] for p in d['paths'].values() for op in p.values())}")
s, d, t = call("/api/prefs/ali"); print(f"prefs {s} (503 expected without DATABASE_URL) {d}")
s, d, t = call("/api/chat", {"messages": [{"role": "user", "content": "hi"}]}); print(f"chat {s} (503 expected without ANTHROPIC_API_KEY) {d}")
s, d, t = call("/api/neighborhood/nowhere"); print(f"404 check {s} {d}")
