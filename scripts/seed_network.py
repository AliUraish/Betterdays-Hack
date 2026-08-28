"""Synthetic network + listings for the 'find a place through your network' mode.
Deterministic (seeded RNG). Postgres = people/connections/listings; ClickHouse = rent benchmarks + events table."""
import json, os, random, sys, uuid
from pathlib import Path
from urllib.parse import quote
from dotenv import load_dotenv
import psycopg
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ch import client as ch_client
ROOT = Path(__file__).resolve().parent.parent; load_dotenv(ROOT / ".env")
random.seed(7)
PG = os.getenv("DATABASE_URL") or f"postgresql://{os.getenv('PGUSER','postgres')}:{quote(os.getenv('PGPASSWORD',''),safe='')}@{os.getenv('PGHOST')}:{os.getenv('PGPORT','5432')}/{os.getenv('PGDATABASE','postgres')}?sslmode=require"
DEMO_USER = "demo"

FIRST = ["Priya","Marcus","Elena","Jamal","Sofia","Kenji","Amara","Diego","Hannah","Ravi","Lucia","Owen","Mei","Tomás","Zara","Noah","Ines","Kwame","Yuki","Leo",
         "Aisha","Felix","Nadia","Omar","Grace","Ivan","Chloe","Arjun","Maya","Sam","Rosa","Ethan","Fatima","Jonas","Lila","Mateo","Nia","Theo","Vera","Wes"]
LAST = ["Shah","Bell","Rossi","Carter","Alvarez","Tanaka","Okafor","Mendes","Kim","Iyer","Ferrer","Walsh","Chen","Reyes","Haddad","Park","Costa","Boateng","Sato","Novak",
        "Rahman","Berg","Petrov","Farah","Lee","Sokolov","Dubois","Nair","Singh","Ortiz","Moreno","Hughes","Zaidi","Weber","Sun","Silva","Adeyemi","Lund","Bauer","Grant"]
COMPANIES = [("Stripe","Software Engineer"),("UCSF","Nurse Practitioner"),("Salesforce","Product Manager"),("SF Unified","Teacher"),("Anthropic","Research Engineer"),
             ("Databricks","Data Engineer"),("Kaiser","Physician"),("Airbnb","Designer"),("Notion","Growth Lead"),("Figma","Engineer"),("Uber","Analyst"),("Twitch","PM"),
             ("Sutter Health","RN"),("OpenAI","Engineer"),("Chime","Backend Engineer"),("Gusto","Recruiter"),("Cruise","Robotics Engineer"),("Plaid","Solutions Engineer")]
SF_LOCS = ["San Francisco, CA","SF","San Francisco","Bay Area","San Francisco 🌉","94110","SF Mission","Sunset District, SF","Oakland, CA","Berkeley, CA","New York, NY","Austin, TX","Seattle, WA","","London","Los Angeles, CA"]
NBHDS_ROOM = ["Mission","Castro","Inner Sunset","Inner Richmond","Hayes Valley","Bernal Heights","Potrero Hill","Noe Valley","Lower Haight","Outer Sunset","Marina","Russian Hill","Dogpatch","Glen Park","Cole Valley","Duboce Triangle","Nob Hill","Pacific Heights","Excelsior","Outer Richmond"]
TIER = {"premium":["Marina","Pacific Heights","Noe Valley","Russian Hill","Cow Hollow","Presidio Heights","Hayes Valley","Cole Valley","Telegraph Hill","Lower Pacific Heights","Dolores Heights"],
        "value":["Outer Sunset","Outer Richmond","Excelsior","Bayview","Portola","Visitacion Valley","Ingleside","Crocker Amazon","Tenderloin","Sunnydale","Oceanview","Mission Terrace","Cayuga","Parkside","Lakeshore","Hunters Point","Silver Terrace","University Mound"]}

def main():
    with psycopg.connect(PG) as c:
        c.execute("""
        DROP TABLE IF EXISTS verdicts, messages, negotiations, invites, listings, connections, people, user_profile CASCADE;
        CREATE TABLE people (id text PRIMARY KEY, name text, headline text, company text, linkedin_url text, x_handle text, x_location text,
                             email text, is_member boolean DEFAULT false, neighborhood text, color text);
        CREATE TABLE connections (person_a text REFERENCES people(id), person_b text REFERENCES people(id), source text DEFAULT 'linkedin',
                                  PRIMARY KEY (person_a, person_b));
        CREATE TABLE listings (id serial PRIMARY KEY, owner_id text REFERENCES people(id), owner_name text, owner_email text, source text,
                               url text, title text, neighborhood text, address text, lat double precision, lon double precision,
                               rent int, room_type text, move_in date, features jsonb, description text, reservation_price int,
                               shared_by text REFERENCES people(id), created_at timestamptz DEFAULT now());
        CREATE TABLE invites (id serial PRIMARY KEY, user_id text, listing_id int REFERENCES listings(id), to_email text, to_name text,
                              token text UNIQUE, subject text, body text, status text DEFAULT 'sent', sent_at timestamptz DEFAULT now(), accepted_at timestamptz);
        CREATE TABLE negotiations (id serial PRIMARY KEY, user_id text, listing_id int REFERENCES listings(id), status text DEFAULT 'open',
                                   last_offer int, owner_last_offer int, created_at timestamptz DEFAULT now());
        CREATE TABLE messages (id serial PRIMARY KEY, negotiation_id int REFERENCES negotiations(id), role text, content text, offer int, created_at timestamptz DEFAULT now());
        CREATE TABLE verdicts (id serial PRIMARY KEY, negotiation_id int REFERENCES negotiations(id), fit numeric, fair_low int, fair_high int,
                               suggested int, verdict text, reason text, created_at timestamptz DEFAULT now());
        CREATE TABLE user_profile (user_id text PRIMARY KEY, name text, linkedin_url text, x_handle text, budget int, room_type text,
                                   move_in date, must_haves jsonb, weights jsonb, updated_at timestamptz DEFAULT now());
        """)
        nb = {r[0]: (r[1], r[2]) for r in c.execute("SELECT name, centroid_lat, centroid_lon FROM neighborhoods").fetchall()}
        # --- people: you + 40
        people = [(DEMO_USER, "You", "Looking for a place in SF", "—", "https://linkedin.com/in/you", "@you", "San Francisco, CA", "you@example.com", True, "Mission", "#3987e5")]
        colors = ["#3987e5","#d95926","#199e70","#c98500","#d55181","#9085e9","#e66767","#2a78d6"]
        for i in range(40):
            f, l = FIRST[i], LAST[(i * 7) % 40]; comp, role = COMPANIES[i % len(COMPANIES)]
            loc = SF_LOCS[i % len(SF_LOCS)] if i % 3 else "San Francisco, CA"
            member = (i % 5 in (0, 1, 3)) if i < 20 else (i % 3 == 0)
            pid = f"p{i+1:02d}"
            people.append((pid, f"{f} {l}", f"{role} at {comp}", comp, f"https://linkedin.com/in/{f.lower()}-{l.lower()}", f"@{f.lower()}{l.lower()[:3]}",
                           loc, f"{f.lower()}.{l.lower()}@example.com", member, random.choice(NBHDS_ROOM) if "S" in loc or "9" in loc else None, colors[i % 8]))
        # owners of "shared"/web listings become real people in the network (members, reachable via a bridge)
        extra = [("p41","Dana Kowalski","Nurse at UCSF","UCSF","https://linkedin.com/in/dana-kowalski","@danak","San Francisco, CA","dana.k@example.com",True,"Castro","#199e70"),
                 ("p42","Jordan Lin","Firmware Engineer at Cruise","Cruise","https://linkedin.com/in/jordan-lin","@jlin","SF","j.lin@example.com",True,"Potrero Hill","#c98500"),
                 ("p43","Riley Moss","Teacher at SF Unified","SF Unified","https://linkedin.com/in/riley-moss","@rmoss","Noe Valley, SF","r.moss@example.com",True,"Noe Valley","#d55181"),
                 ("p44","Casey Tran","Barista / illustrator","Ritual Coffee","https://linkedin.com/in/casey-tran","@caseyt","Lower Haight, SF","lh.flat@example.com",True,"Lower Haight","#9085e9"),
                 ("p45","Luis Ortega","Bus operator at SFMTA","SFMTA","https://linkedin.com/in/luis-ortega","@luiso","Excelsior 94112","exc.room@example.com",True,"Excelsior","#e66767")]
        people += extra
        c.cursor().executemany("INSERT INTO people VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", people)
        # --- connections: you -> 14 first-degree; each first-degree -> 2-4 others (2nd degree); some mutual (verified)
        edges = set()
        first = [f"p{i:02d}" for i in range(1, 15)]
        for p in first:
            edges.add((DEMO_USER, p))
            if random.random() < 0.7: edges.add((p, DEMO_USER))        # mutual = verified
        others = [f"p{i:02d}" for i in range(15, 41)]
        for p in first:
            for q in random.sample(others, random.randint(2, 4)):
                edges.add((p, q))
                if random.random() < 0.6: edges.add((q, p))
        for a, b in (("p01","p41"),("p41","p01"),("p02","p42"),("p03","p43"),("p43","p03"),("p05","p44"),("p09","p45"),("p45","p09")):
            edges.add((a, b))                      # bridges to the new listing owners (some mutual = verified)
        for _ in range(12):  # a few links among 2nd-degree people
            a, b = random.sample(others, 2); edges.add((a, b))
        c.cursor().executemany("INSERT INTO connections (person_a, person_b) VALUES (%s,%s) ON CONFLICT DO NOTHING", list(edges))
        # --- listings
        def L(owner, source, nbhd, rent, room, feats, title, desc, shared_by=None, url=None, owner_email=None, owner_name=None):
            lat, lon = nb[nbhd]; lat += random.uniform(-0.003, 0.003); lon += random.uniform(-0.004, 0.004)
            return (owner, owner_name, owner_email, source, url, title, nbhd, None, lat, lon, rent, room, f"2026-{random.randint(9,11):02d}-01",
                    json.dumps(feats), desc, int(rent * random.uniform(0.86, 0.95)), shared_by)
        F = lambda **k: {"pets": False, "furnished": False, "parking": False, "laundry": True, "private_bath": False, **k}
        rows = [
            L("p03", "member", "Mission", 1650, "room", F(pets=True), "Sunny room in 3BR Victorian", "Big room, two chill roommates, near 24th St BART.", ),
            L("p07", "member", "Inner Sunset", 1450, "room", F(laundry=True), "Room near Golden Gate Park", "Quiet block, N-Judah 2 min, garden.", ),
            L("p17", "member", "Bernal Heights", 1800, "room", F(private_bath=True, parking=True), "Master bedroom w/ private bath", "Top of Bernal, views, parking spot included."),
            L("p22", "member", "Hayes Valley", 2750, "studio", F(furnished=True), "Furnished studio, 6-month lease", "Walk to everything. Available Oct 1."),
            L("p31", "member", "Outer Sunset", 1250, "room", F(pets=True, laundry=True), "Room 4 blocks from Ocean Beach", "Surfers welcome. Dog-friendly."),
            L("p41", "shared", "Castro", 1900, "room", F(laundry=True), "Room in 2BR, Castro", "Priya's friend Dana is moving out of the other room.", shared_by="p01"),
            L("p42", "shared", "Potrero Hill", 1700, "room", F(parking=True), "Room, Potrero Hill, parking", "Shared by Marcus — his coworker Jordan's place.", shared_by="p02"),
            L("p43", "shared", "Noe Valley", 2100, "room", F(pets=True, private_bath=True), "Quiet room in Noe, cat ok", "Elena's neighbor Riley is renting a room.", shared_by="p03"),
            L("p44", "url", "Lower Haight", 1550, "room", F(), "Room in Lower Haight flat", "Found on craigslist — turns out Sofia knows the lister.", url="https://sfbay.craigslist.org/sfc/roo/d/room-lower-haight/770001.html", shared_by="p05"),
            L(None, "url", "Russian Hill", 3200, "1br", F(laundry=True), "1BR with bay view", "Imported from zillow.", url="https://www.zillow.com/homedetails/russian-hill-1br/2001", owner_email="rh.owner@example.com", owner_name="Owner"),
            L("p45", "url", "Excelsior", 1200, "room", F(pets=True, parking=True), "Cheap room, Excelsior", "Found on craigslist — Hannah's cousin Luis.", url="https://sfbay.craigslist.org/sfc/roo/d/excelsior-room/770002.html", shared_by="p09"),
            L(None, "url", "Marina", 2400, "room", F(furnished=True, laundry=True), "Furnished room, Marina", "Imported from facebook marketplace.", url="https://www.facebook.com/marketplace/item/8800", owner_email="marina.rm@example.com", owner_name="Listing contact"),
        ]
        c.cursor().executemany("""INSERT INTO listings (owner_id, owner_name, owner_email, source, url, title, neighborhood, address, lat, lon, rent, room_type, move_in, features, description, reservation_price, shared_by)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
        c.execute("UPDATE listings l SET owner_name = p.name, owner_email = p.email FROM people p WHERE l.owner_id = p.id")
        c.execute("""INSERT INTO user_profile (user_id, name, linkedin_url, x_handle, budget, room_type, move_in, must_haves, weights)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                  (DEMO_USER, "You", "https://linkedin.com/in/you", "@you", 1800, "room", "2026-10-01", json.dumps({"laundry": True}),
                   json.dumps({"cleanliness":3,"graffiti":2,"street_safety":3,"quiet":3,"parking":2,"infrastructure":2,"responsiveness":2})))
        n = c.execute("SELECT count(*) FROM people").fetchone()[0]; e = c.execute("SELECT count(*) FROM connections").fetchone()[0]; l = c.execute("SELECT count(*) FROM listings").fetchone()[0]
        print(f"postgres: people={n} connections={e} listings={l}")
        all_nb = list(nb)

    # --- ClickHouse: rent benchmarks (illustrative) + events stream
    ch = ch_client()
    ch.command("""CREATE TABLE IF NOT EXISTS better_days.rent_benchmarks (neighborhood String, room_type LowCardinality(String), p25 UInt32, p50 UInt32, p75 UInt32)
                  ENGINE = ReplacingMergeTree ORDER BY (neighborhood, room_type)""")
    ch.command("""CREATE TABLE IF NOT EXISTS better_days.events (ts DateTime DEFAULT now(), user_id String, event LowCardinality(String), listing_id UInt32, meta String)
                  ENGINE = MergeTree ORDER BY (user_id, ts)""")
    ch.command("TRUNCATE TABLE better_days.rent_benchmarks")
    bench = []
    for n_ in all_nb:
        base = 2100 if n_ in TIER["premium"] else 1350 if n_ in TIER["value"] else 1700
        for room, mult in (("room", 1.0), ("studio", 1.55), ("1br", 1.95)):
            p50 = int(base * mult); bench.append([n_, room, int(p50 * 0.85), p50, int(p50 * 1.2)])
    ch.insert("better_days.rent_benchmarks", bench, column_names=["neighborhood", "room_type", "p25", "p50", "p75"])
    print("clickhouse: benchmarks", ch.command("SELECT count() FROM better_days.rent_benchmarks"), "| events table ready")

if __name__ == "__main__":
    main()
