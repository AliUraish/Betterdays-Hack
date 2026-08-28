"""Mode 2 — find a place through your network.
Postgres: people / connections / listings / invites / negotiations. ClickHouse: fit-score inputs (311 scores + rent benchmarks) + events."""
import json, os, re, secrets
from collections import deque
from datetime import datetime
from typing import Optional
from urllib.parse import quote
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import psycopg
from psycopg.rows import dict_row
import analytics as A

router = APIRouter(prefix="/api/net", tags=["network"])

def _pg_url():
    if os.getenv("DATABASE_URL"): return os.getenv("DATABASE_URL")
    return (f"postgresql://{os.getenv('PGUSER','postgres')}:{quote(os.getenv('PGPASSWORD',''), safe='')}"
            f"@{os.getenv('PGHOST')}:{os.getenv('PGPORT','5432')}/{os.getenv('PGDATABASE','postgres')}?sslmode=require")
def pg(): return psycopg.connect(_pg_url(), row_factory=dict_row)

SF_PAT = re.compile(r"\b(san francisco|sf|bay area|94\d{3}|sunset|mission|richmond district)\b|🌉", re.I)
def in_sf(loc): return bool(loc and SF_PAT.search(loc))

def track(user_id, event, listing_id=0, meta=None):
    """Fire-and-forget event to ClickHouse (own thread so cross-cloud latency never blocks a request)."""
    import threading
    def _go():
        try: A.ch().insert("better_days.events", [[user_id, event, int(listing_id or 0), json.dumps(meta or {})]], column_names=["user_id", "event", "listing_id", "meta"])
        except Exception as e: print("track failed:", e)
    threading.Thread(target=_go, daemon=True).start()

# ---------------------------------------------------------------- graph
def load_graph():
    with pg() as c:
        people = {r["id"]: r for r in c.execute("SELECT * FROM people")}
        edges = c.execute("SELECT person_a, person_b FROM connections").fetchall()
    adj = {p: set() for p in people}; directed = set()
    for e in edges:
        directed.add((e["person_a"], e["person_b"])); adj[e["person_a"]].add(e["person_b"]); adj[e["person_b"]].add(e["person_a"])
    return people, adj, directed

def bfs(adj, src, max_depth=3):
    dist, parent = {src: 0}, {src: None}; q = deque([src])
    while q:
        u = q.popleft()
        if dist[u] >= max_depth: continue
        for v in adj[u]:
            if v not in dist: dist[v] = dist[u] + 1; parent[v] = u; q.append(v)
    return dist, parent

def path_to(parent, target):
    out = []; cur = target
    while cur is not None: out.append(cur); cur = parent.get(cur)
    return list(reversed(out))

@router.get("/graph/{user_id}", operation_id="network_graph", summary="Your network: 1st/2nd degree with verification badges")
def graph(user_id: str):
    people, adj, directed = load_graph()
    if user_id not in people: user_id = "demo"
    dist, parent = bfs(adj, user_id, 2)
    with pg() as c:
        listings_by_owner = {}
        for r in c.execute("SELECT id, owner_id, shared_by, neighborhood, rent, room_type, title FROM listings"):
            for k in (r["owner_id"], r["shared_by"]):
                if k: listings_by_owner.setdefault(k, []).append({"id": r["id"], "neighborhood": r["neighborhood"], "rent": r["rent"], "room_type": r["room_type"], "title": r["title"], "role": "owner" if k == r["owner_id"] else "shared"})
    nodes = []
    for pid, d in dist.items():
        p = people[pid]
        nodes.append({"id": pid, "name": p["name"], "headline": p["headline"], "company": p["company"], "degree": d, "is_member": p["is_member"],
                      "x_handle": p["x_handle"], "x_location": p["x_location"], "in_sf": in_sf(p["x_location"]),
                      "verified": (user_id, pid) in directed and (pid, user_id) in directed if d == 1 else None,
                      "via": path_to(parent, pid)[1:-1], "color": p["color"], "linkedin_url": p["linkedin_url"], "listings": listings_by_owner.get(pid, [])})
    links = [{"source": a, "target": b} for a in dist for b in adj[a] if b in dist and a < b]
    summary = {"first": sum(1 for n in nodes if n["degree"] == 1), "second": sum(1 for n in nodes if n["degree"] == 2),
               "members": sum(1 for n in nodes if n["is_member"] and n["degree"] > 0), "in_sf": sum(1 for n in nodes if n["in_sf"] and n["degree"] > 0),
               "verified": sum(1 for n in nodes if n["verified"]), "with_listings": sum(1 for n in nodes if n["listings"] and n["degree"] > 0)}
    track(user_id, "network_viewed", meta=summary)
    return {"user": user_id, "nodes": nodes, "links": links, "summary": summary}

# ---------------------------------------------------------------- profile
class Profile(BaseModel):
    user_id: str = "demo"; name: Optional[str] = None; linkedin_url: Optional[str] = None; x_handle: Optional[str] = None
    budget: int = 1800; room_type: str = "room"; move_in: Optional[str] = None; must_haves: dict = {}; weights: Optional[dict] = None

@router.get("/profile/{user_id}", operation_id="get_profile")
def get_profile(user_id: str):
    with pg() as c:
        r = c.execute("SELECT * FROM user_profile WHERE user_id=%s", (user_id,)).fetchone() or c.execute("SELECT * FROM user_profile WHERE user_id='demo'").fetchone()
    return r

@router.post("/profile", operation_id="save_profile")
def save_profile(p: Profile):
    with pg() as c:
        c.execute("""INSERT INTO user_profile (user_id, name, linkedin_url, x_handle, budget, room_type, move_in, must_haves, weights)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (user_id) DO UPDATE SET name=EXCLUDED.name, linkedin_url=EXCLUDED.linkedin_url,
                     x_handle=EXCLUDED.x_handle, budget=EXCLUDED.budget, room_type=EXCLUDED.room_type, move_in=EXCLUDED.move_in,
                     must_haves=EXCLUDED.must_haves, weights=EXCLUDED.weights, updated_at=now()""",
                  (p.user_id, p.name, p.linkedin_url, p.x_handle, p.budget, p.room_type, p.move_in, json.dumps(p.must_haves), json.dumps(p.weights or A.DEFAULT_WEIGHTS)))
    track(p.user_id, "profile_saved", meta={"budget": p.budget, "room_type": p.room_type})
    return {"ok": True}

# ---------------------------------------------------------------- listings + fit
_bench = {"ts": 0, "data": {}}
def benchmarks():
    import time
    if time.time() - _bench["ts"] > 600:
        rows = A.ch().query("SELECT neighborhood, room_type, p25, p50, p75 FROM better_days.rent_benchmarks FINAL").result_rows
        _bench.update(ts=time.time(), data={(n, r): (p25, p50, p75) for n, r, p25, p50, p75 in rows})
    return _bench["data"]

def fit_score(listing, profile, weights):
    """0-100 fit: neighborhood (your weights) + price vs fair range + must-haves."""
    S = A.scores(); nb = S["neighborhoods"].get(listing["neighborhood"])
    nbhd = A.weighted(nb["scores"], weights) if nb else 50.0
    b = benchmarks().get((listing["neighborhood"], listing["room_type"])) or (1400, 1700, 2000)
    p25, p50, p75 = b; rent = listing["rent"]; budget = profile["budget"] or 1800
    price = max(0.0, min(100.0, 100 - (rent / p50 - 1) * 250))
    over = max(0, rent - budget) / budget; price = max(0.0, price - over * 300)
    feats = listing["features"] or {}; missing = [k for k, v in (profile["must_haves"] or {}).items() if v and not feats.get(k)]
    type_ok = listing["room_type"] == (profile["room_type"] or listing["room_type"])
    feat = max(0.0, 100 - 20 * len(missing) - (30 if not type_ok else 0))
    fit = round(0.45 * nbhd + 0.40 * price + 0.15 * feat, 1)
    perfect = nbhd >= 60 and rent <= min(budget, p50 * 1.05) and not missing and type_ok
    return {"fit": fit, "nbhd_score": round(nbhd, 1), "price_score": round(price, 1), "feature_score": round(feat, 1),
            "fair_low": p25, "fair_mid": p50, "fair_high": p75, "over_budget": max(0, rent - budget), "missing": missing, "type_ok": type_ok, "perfect": perfect}

def reach(listing, dist, parent, people):
    owner = listing["owner_id"]; bridge = listing["shared_by"]
    if owner and owner in dist and people[owner]["is_member"]:
        ids = path_to(parent, owner)
        return {"how": "chat", "label": "Chat now", "path": [people[p]["name"] for p in ids], "path_ids": ids, "degree": dist[owner]}
    if bridge and bridge in dist:
        ids = path_to(parent, bridge)
        return {"how": "intro", "label": f"Ask {people[bridge]['name'].split()[0]} for an intro", "path": [people[p]["name"] for p in ids] + [listing["owner_name"] or "owner"], "path_ids": ids + ([owner] if owner else []), "degree": dist[bridge] + 1}
    return {"how": "invite", "label": "Invite owner to Better Days", "path": ["You", listing["owner_name"] or "owner"], "path_ids": [], "degree": None}

@router.get("/listings/{user_id}", operation_id="listings_for_user", summary="Listings reachable through your network, with fit score and how to reach the owner")
def listings(user_id: str):
    people, adj, directed = load_graph()
    gid = user_id if user_id in people else "demo"          # graph identity (seeded demo network); invites/profile stay per real user
    dist, parent = bfs(adj, gid, 3)
    with pg() as c:
        prof = c.execute("SELECT * FROM user_profile WHERE user_id=%s", (user_id,)).fetchone() or c.execute("SELECT * FROM user_profile WHERE user_id='demo'").fetchone()
        rows = c.execute("SELECT * FROM listings ORDER BY id").fetchall()
        inv = {r["listing_id"]: r["status"] for r in c.execute("SELECT listing_id, status FROM invites WHERE user_id=%s ORDER BY id", (user_id,))}
    weights = prof["weights"] or A.DEFAULT_WEIGHTS
    out = []
    for l in rows:
        f = fit_score(l, prof, weights); r = reach(l, dist, parent, people)
        if inv.get(l["id"]) == "accepted": r = {"how": "chat", "label": "Chat now (joined via your invite)", "path": ["You", l["owner_name"]], "degree": 1}
        elif inv.get(l["id"]) == "sent" and r["how"] == "invite": r["label"] = "Invite sent — waiting"
        out.append({**{k: v for k, v in l.items() if k != "reservation_price"}, "move_in": str(l["move_in"]), "fit": f, "reach": r, "invite_status": inv.get(l["id"])})
    out.sort(key=lambda x: -x["fit"]["fit"])
    track(user_id, "listings_viewed", meta={"n": len(out)})
    return {"profile": {k: (str(v) if isinstance(v, datetime) else v) for k, v in prof.items()}, "listings": out}

# ---------------------------------------------------------------- invites (email → outbox if no provider)
class InviteIn(BaseModel):
    user_id: str = "demo"; listing_id: int; note: Optional[str] = None

def send_email(to, subject, body):
    key = os.getenv("RESEND_API_KEY")
    if not key: return "outbox"
    import urllib.request
    req = urllib.request.Request("https://api.resend.com/emails", data=json.dumps({"from": os.getenv("EMAIL_FROM", "Better Days <onboarding@resend.dev>"), "to": [to], "subject": subject, "text": body}).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try: urllib.request.urlopen(req, timeout=15).read(); return "sent"
    except Exception as e: print("email failed:", e); return "outbox"

@router.post("/invite", operation_id="invite_owner", summary="Email a listing owner an invite to join and chat")
def invite(b: InviteIn):
    with pg() as c:
        l = c.execute("SELECT * FROM listings WHERE id=%s", (b.listing_id,)).fetchone()
        if not l: raise HTTPException(404, "listing not found")
        prof = c.execute("SELECT * FROM user_profile WHERE user_id=%s", (b.user_id,)).fetchone() or {"name": "A Better Days user"}
        token = secrets.token_urlsafe(12); link = f"{os.getenv('PUBLIC_URL','http://localhost:8800')}/?join={token}"
        who = prof.get('name') if prof.get('name') and prof.get('name') != 'You' else 'A renter from your network'
        subject = f"{who} wants to chat about your place in {l['neighborhood']}"
        body = (f"Hi {l['owner_name'] or 'there'},\n\n{who} found your listing \"{l['title']}\" ({l['neighborhood']}, ${l['rent']}/mo) "
                f"through their network on Better Days and would like to chat.\n\n{b.note or ''}\n\nJoin and reply here: {link}\n\n— Better Days")
        status = send_email(l["owner_email"], subject, body)
        c.execute("INSERT INTO invites (user_id, listing_id, to_email, to_name, token, subject, body, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                  (b.user_id, b.listing_id, l["owner_email"], l["owner_name"], token, subject, body, "sent"))
    track(b.user_id, "invite_sent", b.listing_id, {"to": l["owner_email"], "delivery": status})
    return {"ok": True, "delivery": status, "to": l["owner_email"], "subject": subject, "body": body, "join_link": link, "token": token}

@router.get("/outbox/{user_id}", operation_id="outbox")
def outbox(user_id: str):
    with pg() as c:
        return c.execute("SELECT id, listing_id, to_email, to_name, subject, body, status, sent_at, accepted_at, token FROM invites WHERE user_id=%s ORDER BY id DESC", (user_id,)).fetchall()

@router.post("/join/{token}", operation_id="accept_invite", summary="Owner clicks the invite link: becomes a member, chat unlocks")
def join(token: str):
    with pg() as c:
        inv = c.execute("SELECT * FROM invites WHERE token=%s", (token,)).fetchone()
        if not inv: raise HTTPException(404, "invalid invite")
        c.execute("UPDATE invites SET status='accepted', accepted_at=now() WHERE id=%s", (inv["id"],))
        l = c.execute("SELECT * FROM listings WHERE id=%s", (inv["listing_id"],)).fetchone()
        if l["owner_id"]: c.execute("UPDATE people SET is_member=true WHERE id=%s", (l["owner_id"],))
    track(inv["user_id"], "invite_accepted", inv["listing_id"])
    return {"ok": True, "listing_id": inv["listing_id"], "owner": l["owner_name"]}

# ---------------------------------------------------------------- negotiation: owner persona + advisor
class NegMsg(BaseModel):
    user_id: str = "demo"; text: str; offer: Optional[int] = None

FAST_MODEL = os.getenv("OPENAI_FAST_MODEL", "gpt-5.4-mini")

def _llm_json(instructions, user_text, schema_name, schema):
    from openai import OpenAI
    client = OpenAI()
    r = client.responses.create(model=FAST_MODEL, instructions=instructions, input=user_text,
                                text={"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}})
    return json.loads(r.output_text)

def advise(listing, prof, weights, history, owner_offer):
    f = fit_score(listing, prof, weights); budget = prof["budget"] or 1800
    ask = owner_offer or listing["rent"]
    target = min(budget, int(f["fair_mid"] * 0.97)); walk = min(budget, int(f["fair_high"]))
    if f["perfect"] and ask <= min(budget, f["fair_mid"] * 1.05): verdict, suggested = "ACCEPT", ask
    elif ask <= min(budget, f["fair_mid"]): verdict, suggested = "ACCEPT", ask
    elif ask <= walk and f["nbhd_score"] >= 45 and f["type_ok"] and not f["missing"]: verdict, suggested = "COUNTER", max(target, min(ask - 100, int(ask * 0.94)))
    else: verdict, suggested = "WALK_AWAY", target
    why = []
    why.append(f"neighborhood scores {f['nbhd_score']:.0f}/100 on your weights")
    why.append(f"${ask} vs fair range ${f['fair_low']}–${f['fair_high']} (median ${f['fair_mid']})")
    if f["over_budget"]: why.append(f"${f['over_budget']} over your ${budget} budget")
    if f["missing"]: why.append("missing must-haves: " + ", ".join(f["missing"]))
    if not f["type_ok"]: why.append(f"it's a {listing['room_type']}, you want a {prof['room_type']}")
    plan = {"ACCEPT": f"Take it at ${ask}.", "COUNTER": f"Offer ${suggested}; settle anywhere up to ${walk}.", "WALK_AWAY": f"Don't pay more than ${walk} here — aim for ${target}."}[verdict]
    return {"fit": f["fit"], "nbhd_score": f["nbhd_score"], "fair_low": f["fair_low"], "fair_mid": f["fair_mid"], "fair_high": f["fair_high"],
            "asking": ask, "budget": budget, "target": target, "suggested": suggested, "walk_away_above": walk, "verdict": verdict,
            "reason": "; ".join(why), "plan": plan, "perfect": f["perfect"], "missing": f["missing"]}

@router.get("/advice/{listing_id}", operation_id="advice", summary="Advisor's opening plan for a listing: what to offer, ceiling, verdict")
def advice(listing_id: int, user_id: str = "demo"):
    with pg() as c:
        l = c.execute("SELECT * FROM listings WHERE id=%s", (listing_id,)).fetchone()
        if not l: raise HTTPException(404, "listing not found")
        prof = c.execute("SELECT * FROM user_profile WHERE user_id=%s", (user_id,)).fetchone() or c.execute("SELECT * FROM user_profile WHERE user_id='demo'").fetchone()
        n = c.execute("SELECT owner_last_offer FROM negotiations WHERE user_id=%s AND listing_id=%s ORDER BY id DESC LIMIT 1", (user_id, listing_id)).fetchone()
    return advise(l, prof, prof["weights"] or A.DEFAULT_WEIGHTS, [], (n or {}).get("owner_last_offer") or l["rent"])

@router.get("/negotiation/{listing_id}", operation_id="get_negotiation")
def get_negotiation(listing_id: int, user_id: str = "demo"):
    with pg() as c:
        n = c.execute("SELECT * FROM negotiations WHERE user_id=%s AND listing_id=%s ORDER BY id DESC LIMIT 1", (user_id, listing_id)).fetchone()
        if not n: return {"negotiation": None, "messages": [], "verdict": None}
        msgs = c.execute("SELECT role, content, offer, created_at FROM messages WHERE negotiation_id=%s ORDER BY id", (n["id"],)).fetchall()
        v = c.execute("SELECT * FROM verdicts WHERE negotiation_id=%s ORDER BY id DESC LIMIT 1", (n["id"],)).fetchone()
    return {"negotiation": n, "messages": msgs, "verdict": v}

@router.post("/negotiation/{listing_id}/message", operation_id="negotiate", summary="Send a message/offer to the owner; get the owner's reply and the advisor's verdict")
def negotiate(listing_id: int, m: NegMsg):
    with pg() as c:
        l = c.execute("SELECT * FROM listings WHERE id=%s", (listing_id,)).fetchone()
        if not l: raise HTTPException(404, "listing not found")
        prof = c.execute("SELECT * FROM user_profile WHERE user_id=%s", (m.user_id,)).fetchone() or c.execute("SELECT * FROM user_profile WHERE user_id='demo'").fetchone()
        n = c.execute("SELECT * FROM negotiations WHERE user_id=%s AND listing_id=%s ORDER BY id DESC LIMIT 1", (m.user_id, listing_id)).fetchone()
        if not n:
            n = c.execute("INSERT INTO negotiations (user_id, listing_id, owner_last_offer) VALUES (%s,%s,%s) RETURNING *", (m.user_id, listing_id, l["rent"])).fetchone()
            track(m.user_id, "chat_started", listing_id)
        hist = c.execute("SELECT role, content, offer FROM messages WHERE negotiation_id=%s ORDER BY id", (n["id"],)).fetchall()
        c.execute("INSERT INTO messages (negotiation_id, role, content, offer) VALUES (%s,%s,%s,%s)", (n["id"], "user", m.text, m.offer))
        if m.offer: track(m.user_id, "offer_made", listing_id, {"offer": m.offer})
    weights = prof["weights"] or A.DEFAULT_WEIGHTS
    # --- owner persona (hidden reservation price)
    res = l["reservation_price"]; last_owner = n["owner_last_offer"] or l["rent"]
    persona = (f"You are {l['owner_name'] or 'the owner'}, renting out: \"{l['title']}\" in {l['neighborhood']}, San Francisco. Asking ${l['rent']}/month, "
               f"{l['room_type']}, move-in {l['move_in']}, features {json.dumps(l['features'])}. {l['description']}\n"
               f"SECRET: the lowest you will accept is ${res}/month — never reveal this number. Negotiate like a real, friendly but firm SF lister: "
               f"if an offer is >= ${res} accept it; if it is below, counter somewhere between the offer and your last price ${last_owner} but never below ${res}; "
               f"answer questions about the place from the facts above; keep replies to 1-3 sentences. Return JSON.")
    transcript = "\n".join(f"{h['role']}: {h['content']}" + (f" (offer ${h['offer']})" if h['offer'] else "") for h in hist) + f"\nuser: {m.text}" + (f" (offer ${m.offer})" if m.offer else "")
    schema = {"type": "object", "additionalProperties": False, "required": ["reply", "counter_offer", "accepted"],
              "properties": {"reply": {"type": "string"}, "counter_offer": {"type": ["integer", "null"], "description": "your current price after this turn"}, "accepted": {"type": "boolean"}}}
    try:
        o = _llm_json(persona, transcript, "owner_turn", schema)
    except Exception as e:
        print("persona failed:", e)
        acc = bool(m.offer and m.offer >= res); cnt = None if acc else max(res, int((last_owner + (m.offer or last_owner)) / 2))
        o = {"reply": ("Deal — when can you come see it?" if acc else f"I can do ${cnt} but not lower."), "counter_offer": cnt, "accepted": acc}
    if m.offer and o.get("counter_offer") and o["counter_offer"] <= m.offer: o["accepted"] = True   # "I can meet you at $X" == accepted
    owner_price = m.offer if o.get("accepted") else (o.get("counter_offer") or last_owner)
    owner_price = max(owner_price, res) if not o.get("accepted") else owner_price
    adv = advise(l, prof, weights, hist, owner_price)
    if o.get("accepted"): adv["verdict"] = "ACCEPT" if adv["verdict"] != "WALK_AWAY" else adv["verdict"]; adv["asking"] = owner_price
    with pg() as c:
        c.execute("INSERT INTO messages (negotiation_id, role, content, offer) VALUES (%s,%s,%s,%s)", (n["id"], "owner", o["reply"], owner_price))
        c.execute("INSERT INTO verdicts (negotiation_id, fit, fair_low, fair_high, suggested, verdict, reason) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                  (n["id"], adv["fit"], adv["fair_low"], adv["fair_high"], adv["suggested"], adv["verdict"], adv["reason"]))
        c.execute("UPDATE negotiations SET last_offer=%s, owner_last_offer=%s, status=%s WHERE id=%s", (m.offer, owner_price, "agreed" if o.get("accepted") else "open", n["id"]))
    track(m.user_id, "owner_replied", listing_id, {"price": owner_price, "accepted": bool(o.get("accepted"))})
    track(m.user_id, "verdict", listing_id, {"verdict": adv["verdict"], "fit": adv["fit"]})
    if o.get("accepted"): track(m.user_id, "deal", listing_id, {"price": owner_price})
    return {"owner": {"reply": o["reply"], "price": owner_price, "accepted": bool(o.get("accepted"))}, "advisor": adv}

# ---------------------------------------------------------------- funnel (ClickHouse)
@router.get("/funnel/{user_id}", operation_id="funnel")
def funnel(user_id: str):
    rows = A.ch().query("SELECT event, count() FROM better_days.events WHERE user_id={u:String} GROUP BY event", parameters={"u": user_id}).result_rows
    counts = dict(rows)
    return {"reach": None, "invites": counts.get("invite_sent", 0), "joined": counts.get("invite_accepted", 0), "chats": counts.get("chat_started", 0),
            "offers": counts.get("offer_made", 0), "deals": counts.get("deal", 0), "events": counts}
