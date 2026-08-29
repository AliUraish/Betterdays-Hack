"""Better Days API — find your perfect SF neighborhood from 311 data.
ClickHouse = analytics, Postgres = user prefs, Claude = chat with tools. Port 8800."""
import json, os
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import analytics as A
import network as N

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8800")
app = FastAPI(title="Better Days — SF Neighborhood Finder", version="0.1.0",
              description="Scores 117 San Francisco neighborhoods on livability dimensions derived from 311 cases (ClickHouse).",
              servers=[{"url": PUBLIC_URL}])
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(N.router)

# ---------------------------------------------------------------- Postgres (optional: prefs / saved)
def _pg_url():
    if os.getenv("DATABASE_URL"): return os.getenv("DATABASE_URL")
    if os.getenv("PGHOST"):
        from urllib.parse import quote
        return (f"postgresql://{os.getenv('PGUSER','postgres')}:{quote(os.getenv('PGPASSWORD',''), safe='')}"
                f"@{os.getenv('PGHOST')}:{os.getenv('PGPORT','5432')}/{os.getenv('PGDATABASE','postgres')}?sslmode=require")
    return None
PG_URL = _pg_url()
def pg():
    if not PG_URL: raise HTTPException(503, "DATABASE_URL not configured (Postgres)")
    import psycopg
    return psycopg.connect(PG_URL)

@app.on_event("startup")
def _init_pg():
    if not PG_URL: return
    with pg() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS prefs (id serial PRIMARY KEY, user_id text NOT NULL,
                     weights jsonb NOT NULL, near text, created_at timestamptz DEFAULT now())""")
        c.execute("""CREATE TABLE IF NOT EXISTS saved_neighborhoods (id serial PRIMARY KEY, user_id text NOT NULL,
                     neighborhood text NOT NULL, note text, score numeric, created_at timestamptz DEFAULT now())""")
        c.execute("""CREATE TABLE IF NOT EXISTS chat_log (id serial PRIMARY KEY, user_id text, question text,
                     answer text, tools jsonb, created_at timestamptz DEFAULT now())""")

# ---------------------------------------------------------------- models
class Weights(BaseModel):
    cleanliness: int = Field(3, ge=0, le=5); graffiti: int = Field(2, ge=0, le=5)
    street_safety: int = Field(3, ge=0, le=5); quiet: int = Field(3, ge=0, le=5)
    parking: int = Field(2, ge=0, le=5); infrastructure: int = Field(2, ge=0, le=5)
    responsiveness: int = Field(2, ge=0, le=5)

class RecommendIn(BaseModel):
    weights: Weights = Weights()
    near: Optional[str] = Field(None, description="Neighborhood name or 'lat,lon' to stay close to")
    max_km: float = Field(3.0, description="Radius when `near` is given")
    top_n: int = Field(5, ge=1, le=50)
    residential_only: bool = True
    date_from: str = A.DEFAULT_FROM
    date_to: str = A.DEFAULT_TO

class PrefsIn(BaseModel):
    user_id: str; weights: Weights = Weights(); near: Optional[str] = None
class SavedIn(BaseModel):
    user_id: str; neighborhood: str; note: Optional[str] = None; score: Optional[float] = None
class ChatMsg(BaseModel):
    role: str; content: str
class ChatIn(BaseModel):
    messages: list[ChatMsg]
    weights: Optional[Weights] = None
    user_id: Optional[str] = None

# ---------------------------------------------------------------- analytics endpoints
@app.get("/api/health", operation_id="health", tags=["meta"])
def health():
    return A.health()

@app.get("/api/meta", operation_id="meta", tags=["meta"])
def meta():
    return {"dimensions": A.DIMENSIONS, "default_weights": A.DEFAULT_WEIGHTS,
            "window": {"from": A.DEFAULT_FROM, "to": A.DEFAULT_TO},
            "neighborhoods": [{"name": n, "residential": v["residential"]} for n, v in A.neighborhoods().items()]}

@app.get("/api/neighborhoods.geojson", operation_id="neighborhoods_geojson", tags=["map"])
def neighborhoods_geojson(date_from: str = A.DEFAULT_FROM, date_to: str = A.DEFAULT_TO):
    S = A.scores(date_from, date_to)
    feats = []
    for n, nb in A.neighborhoods().items():
        v = S["neighborhoods"][n]
        feats.append({"type": "Feature", "id": nb["id"],
                      "geometry": {"type": "MultiPolygon", "coordinates": nb["geometry"]},
                      "properties": {"name": n, "residential": nb["residential"], "area_km2": round(nb["area_km2"], 2),
                                     "centroid": nb["centroid"], "total_cases": v["total_cases"],
                                     "scores": v["scores"], "rates": v["rates"], "counts": v["counts"],
                                     "p50_hours": v["p50_hours"]}})
    return {"type": "FeatureCollection", "window": S["window"], "city_median": S["city_median"], "features": feats}

@app.get("/api/heatmap", operation_id="heatmap", tags=["map"])
def heatmap(dimension: str = "all", precision: int = 7, date_from: str = A.DEFAULT_FROM, date_to: str = A.DEFAULT_TO):
    if dimension != "all" and dimension not in A.RATE_DIMS and dimension != "other":
        raise HTTPException(400, f"unknown dimension {dimension}")
    return A.heatmap(dimension, precision, date_from, date_to)

@app.get("/api/trends", operation_id="trends", tags=["map"])
def trends(dimension: str = "all", date_from: str = A.DEFAULT_FROM, date_to: str = A.DEFAULT_TO):
    return A.trends(dimension, date_from, date_to)

@app.post("/api/recommend", operation_id="recommend", tags=["agent"],
          summary="Rank SF neighborhoods by weighted livability score",
          description="Weights 0-5 per dimension (0 = ignore). Optionally stay within max_km of a neighborhood name or 'lat,lon'.")
def recommend(body: RecommendIn):
    return A.recommend(body.weights.model_dump(), body.near, body.top_n, body.max_km, body.residential_only,
                       body.date_from, body.date_to)

@app.get("/api/neighborhood/{name}", operation_id="neighborhood", tags=["agent"],
         summary="Detailed 311 profile of one neighborhood")
def neighborhood(name: str, date_from: str = A.DEFAULT_FROM, date_to: str = A.DEFAULT_TO):
    d = A.neighborhood_detail(name, date_from, date_to)
    if not d: raise HTTPException(404, f"no neighborhood matching '{name}'")
    return d

@app.get("/api/compare", operation_id="compare", tags=["agent"], summary="Compare two neighborhoods side by side")
def compare(a: str, b: str, date_from: str = A.DEFAULT_FROM, date_to: str = A.DEFAULT_TO):
    d = A.compare(a, b, date_from, date_to)
    if not d: raise HTTPException(404, "neighborhood not found")
    return d

# ---------------------------------------------------------------- Postgres endpoints
@app.post("/api/prefs", operation_id="save_prefs", tags=["user"])
def save_prefs(body: PrefsIn):
    with pg() as c:
        row = c.execute("INSERT INTO prefs (user_id, weights, near) VALUES (%s, %s, %s) RETURNING id, created_at",
                        (body.user_id, json.dumps(body.weights.model_dump()), body.near)).fetchone()
    return {"id": row[0], "created_at": row[1]}

@app.get("/api/prefs/{user_id}", operation_id="get_prefs", tags=["user"])
def get_prefs(user_id: str):
    with pg() as c:
        row = c.execute("SELECT weights, near, created_at FROM prefs WHERE user_id=%s ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    if not row: return {"weights": A.DEFAULT_WEIGHTS, "near": None, "created_at": None}
    return {"weights": row[0], "near": row[1], "created_at": row[2]}

@app.post("/api/saved", operation_id="save_neighborhood", tags=["user"])
def save_neighborhood(body: SavedIn):
    n = A.resolve_name(body.neighborhood)
    if not n: raise HTTPException(404, "neighborhood not found")
    with pg() as c:
        row = c.execute("INSERT INTO saved_neighborhoods (user_id, neighborhood, note, score) VALUES (%s,%s,%s,%s) RETURNING id",
                        (body.user_id, n, body.note, body.score)).fetchone()
    return {"id": row[0], "neighborhood": n}

@app.get("/api/saved/{user_id}", operation_id="list_saved", tags=["user"])
def list_saved(user_id: str):
    with pg() as c:
        rows = c.execute("SELECT neighborhood, note, score, created_at FROM saved_neighborhoods WHERE user_id=%s ORDER BY id DESC", (user_id,)).fetchall()
    return [{"neighborhood": r[0], "note": r[1], "score": float(r[2]) if r[2] is not None else None, "created_at": r[3]} for r in rows]

# ---------------------------------------------------------------- chat (LLM + tools)
# Provider: OpenAI if OPENAI_API_KEY is set (the team's key), else Anthropic if ANTHROPIC_API_KEY is set.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5")
SYSTEM = f"""You are the Better Days neighborhood guide for San Francisco. You help people find a neighborhood that fits
how they want to live, using the city's 311 service-request data ({A.DEFAULT_FROM[:4]}–{A.DEFAULT_TO[:4]}) analysed in ClickHouse.

Dimensions (each scored 0-100 per neighborhood, 100 = best, from complaint density per km²/year, or closure time relative
to the city for responsiveness): {", ".join(f"{k} ({v['desc']})" for k, v in A.DIMENSIONS.items())}.

Turn what the user says into weights 0-5 per dimension (0 = they don't care, 5 = top priority) and call `recommend`.
If they mention a place, pass it as `near`. Use `neighborhood` for follow-ups about one place and `compare` for two.
Answer concisely (under 150 words unless asked for more), name the top picks with their score and one concrete
data point each, and be honest that 311 complaints are a proxy — they reflect what residents report, not everything.
Never invent numbers; only use tool results."""

TOOL_SPECS = [
    {"name": "recommend", "description": "Rank SF neighborhoods by weighted livability score. Weights 0-5 per dimension. Optional `near` (neighborhood name or 'lat,lon') with `max_km` radius.",
     "parameters": {"type": "object", "properties": {
         "weights": {"type": "object", "properties": {d: {"type": "integer", "minimum": 0, "maximum": 5} for d in A.DIMENSIONS}},
         "near": {"type": "string"}, "max_km": {"type": "number"}, "top_n": {"type": "integer"}}, "required": ["weights"]}},
    {"name": "neighborhood", "description": "Detailed 311 profile of one neighborhood: scores, complaint rates, top request types, closure times, yearly trend.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "compare", "description": "Compare two neighborhoods side by side.",
     "parameters": {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["a", "b"]}},
]

def run_tool(name, inp):
    if name == "recommend":
        return A.recommend(inp.get("weights") or {}, inp.get("near"), int(inp.get("top_n") or 5), float(inp.get("max_km") or 3.0))
    if name == "neighborhood":
        return A.neighborhood_detail(inp["name"]) or {"error": f"no neighborhood matching {inp['name']}"}
    if name == "compare":
        return A.compare(inp["a"], inp["b"]) or {"error": "neighborhood not found"}
    return {"error": f"unknown tool {name}"}

def _slim(name, out):
    """Trim tool output for the model (drop geometry-ish bulk, keep numbers)."""
    if name == "recommend":
        return {**out, "results": [{k: v for k, v in r.items() if k != "centroid"} for r in out["results"]]}
    if name == "compare" and "a" in out:
        return {"a": _slim("neighborhood", out["a"]), "b": _slim("neighborhood", out["b"]),
                "score_delta_a_minus_b": out["score_delta_a_minus_b"], "better_a": out["better_a"], "better_b": out["better_b"]}
    if name == "neighborhood" and "centroid" in out:
        return {k: v for k, v in out.items() if k not in ("centroid",)}
    return out

def _handle_call(name, inp, trace, state):
    out = run_tool(name, inp)
    if name == "recommend": state["highlight"] = [r["name"] for r in out["results"]]
    elif name == "neighborhood" and "name" in out: state["highlight"] = [out["name"]]
    elif name == "compare" and "a" in out: state["highlight"] = [out["a"]["name"], out["b"]["name"]]
    slim = _slim(name, out)
    trace.append({"tool": name, "input": inp, "output": slim})
    return json.dumps(slim, default=str)

def _chat_openai(system, history):
    """OpenAI Responses API tool loop (required for gpt-5.x reasoning models with function tools)."""
    from openai import OpenAI
    client = OpenAI()
    tools = [{"type": "function", "name": t["name"], "description": t["description"], "parameters": t["parameters"]} for t in TOOL_SPECS]
    trace, state = [], {"highlight": []}
    resp = client.responses.create(model=OPENAI_MODEL, instructions=system, input=history, tools=tools)
    for _ in range(6):
        calls = [o for o in resp.output if o.type == "function_call"]
        if not calls:
            break
        outputs = []
        for c in calls:
            inp = json.loads(c.arguments or "{}")
            outputs.append({"type": "function_call_output", "call_id": c.call_id,
                            "output": _handle_call(c.name, inp, trace, state)})
        resp = client.responses.create(model=OPENAI_MODEL, previous_response_id=resp.id, input=outputs, tools=tools)
    return resp.output_text or "", trace, state["highlight"], resp.model

KEYWORDS = {
    "cleanliness": ["clean", "tidy", "trash", "garbage", "dirty", "litter"], "graffiti": ["graffiti", "tagging", "vandal"],
    "street_safety": ["safe", "safety", "encampment", "homeless", "tent"], "quiet": ["quiet", "noise", "noisy", "peaceful", "calm", "sleep"],
    "parking": ["parking", "car", "drive", "garage"], "infrastructure": ["streetlight", "pothole", "sidewalk", "sewer", "tree", "infrastructure"],
    "responsiveness": ["fix", "fixes", "respond", "responsive", "fast", "quick", "city"],
}
def _chat_rules(history):
    """No-LLM fallback: keyword weights + `near` detection -> recommend/compare, templated reply."""
    import re
    q = history[-1]["content"]; ql = q.lower()
    names = list(A.neighborhoods()); mentioned = sorted([n for n in names if n.lower() in ql], key=lambda n: ql.index(n.lower()))
    trace, highlight = [], []
    if len(mentioned) >= 2 or " vs " in ql or "compare" in ql:
        a, b = (mentioned + [None, None])[:2]
        if a and b:
            out = A.compare(a, b); trace.append({"tool": "compare", "input": {"a": a, "b": b}, "output": _slim("compare", out)}); highlight = [a, b]
            d = out["score_delta_a_minus_b"]; better = out["better_a"]; worse = out["better_b"]
            lines = [f"**{a}** scores {out['a']['overall_score_default_weights']} vs **{b}** {out['b']['overall_score_default_weights']} (default weights)."]
            lines += [f"• {a} is better on: {', '.join(A.DIMENSIONS[x]['label'].lower() for x in better) or 'nothing'}", f"• {b} is better on: {', '.join(A.DIMENSIONS[x]['label'].lower() for x in worse) or 'nothing'}"]
            lines.append(f"Median close time: {out['a']['p50_hours']}h vs {out['b']['p50_hours']}h.")
            return "\n".join(lines), trace, highlight, "rules-fallback", None
    weights = {k: 0 for k in A.DIMENSIONS}
    for k, kws in KEYWORDS.items():
        if any(re.search(r"\b" + w + r"\b", ql) for w in kws): weights[k] = 5
    if not any(weights.values()): weights = dict(A.DEFAULT_WEIGHTS)
    near = None
    m = re.search(r"near (the )?([a-z' /.]+?)(,|\.|$| and | with | that | where )", ql)
    if m: near = A.resolve_name(m.group(2).strip())
    if not near and mentioned: near = mentioned[0]
    out = A.recommend(weights, near, 3, 3.0)
    trace.append({"tool": "recommend", "input": {"weights": weights, "near": near, "top_n": 3}, "output": _slim("recommend", out)})
    highlight = [r["name"] for r in out["results"]]
    if not out["results"]:
        return "I couldn't find neighborhoods matching that radius — try a wider area.", trace, [], "rules-fallback", weights
    pri = [A.DIMENSIONS[k]["label"].lower() for k, v in weights.items() if v >= 5][:3]
    lines = [f"Based on {', '.join(pri) if pri else 'a balanced mix'}{' near ' + near if near else ''}, here are the best fits:"]
    for r in out["results"]:
        top = sorted(r["scores"].items(), key=lambda kv: -kv[1])[:2]
        lines.append(f"**{r['rank']}. {r['name']}** — {r['score']:.0f}/100" + (f", {r['distance_km']} km away" if r.get("distance_km") is not None else "") +
                     f". Strongest: {', '.join(A.DIMENSIONS[k]['label'].lower() + ' ' + str(int(v)) for k, v in top)}; median close time {r['p50_hours']}h.")
    lines.append("(Answered from ClickHouse directly — the language model is offline, so this is a rules-based summary.)")
    return "\n".join(lines), trace, highlight, "rules-fallback", weights

def _chat_anthropic(system, history):
    import anthropic
    client = anthropic.Anthropic()
    tools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in TOOL_SPECS]
    messages = list(history); trace, state = [], {"highlight": []}
    for _ in range(6):
        kwargs = dict(model=CLAUDE_MODEL, max_tokens=8000, system=system, tools=tools, messages=messages)
        try:
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        except TypeError:
            resp = client.messages.create(**kwargs)
        if resp.stop_reason == "refusal":
            return "I can't help with that one — try asking about neighborhoods.", trace, [], resp.model
        if resp.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": resp.content})
        results = [{"type": "tool_result", "tool_use_id": b.id, "content": _handle_call(b.name, dict(b.input), trace, state)}
                   for b in resp.content if b.type == "tool_use"]
        messages.append({"role": "user", "content": results})
    return "".join(b.text for b in resp.content if b.type == "text"), trace, state["highlight"], resp.model

@app.post("/api/chat", operation_id="chat", tags=["agent"], summary="Ask the neighborhood guide (LLM + tools)")
def chat(body: ChatIn):
    provider = "openai" if os.getenv("OPENAI_API_KEY") else "anthropic" if os.getenv("ANTHROPIC_API_KEY") else None
    history = [{"role": m.role, "content": m.content} for m in body.messages if m.role in ("user", "assistant")]
    if not provider: provider = "rules"
    if not history or history[-1]["role"] != "user":
        raise HTTPException(400, "last message must be from the user")
    system = SYSTEM
    if body.weights:
        system += f"\n\nThe user's current slider weights on the map are: {json.dumps(body.weights.model_dump())}. Start from these unless they say otherwise."
    rule_weights = None
    try:
        if provider == "rules": raise RuntimeError("no LLM key configured")
        reply, trace, highlight, model = (_chat_openai if provider == "openai" else _chat_anthropic)(system, history)
    except Exception as e:
        print("LLM chat failed, using rules fallback:", str(e)[:200])
        if provider == "openai" and os.getenv("ANTHROPIC_API_KEY"):
            try: reply, trace, highlight, model = _chat_anthropic(system, history); provider = "anthropic"
            except Exception as e2: print("anthropic fallback failed:", str(e2)[:200]); reply, trace, highlight, model, rule_weights = _chat_rules(history); provider = "rules"
        else:
            reply, trace, highlight, model, rule_weights = _chat_rules(history); provider = "rules"
    weights = next((t["input"].get("weights") for t in trace if t["tool"] == "recommend"), None) or rule_weights
    if PG_URL:
        try:
            with pg() as c:
                c.execute("INSERT INTO chat_log (user_id, question, answer, tools) VALUES (%s,%s,%s,%s)",
                          (body.user_id, body.messages[-1].content, reply, json.dumps([{"tool": t["tool"], "input": t["input"]} for t in trace])))
        except Exception as e:
            print("chat_log insert failed:", e)
    return {"reply": reply, "tools": trace, "highlight": highlight, "weights": weights, "model": model, "provider": provider}

# ---------------------------------------------------------------- static frontend (added later)
FRONT = ROOT / "frontend"
if FRONT.exists():
    app.mount("/static", StaticFiles(directory=FRONT), name="static")
    @app.get("/", include_in_schema=False)
    def index(): return FileResponse(FRONT / "index.html")
