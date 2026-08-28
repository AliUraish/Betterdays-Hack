"""Load ClickHouse (local or Cloud, per .env). Idempotent: truncates and reloads.

Source of the 311 data:
  --from-postgres   (default when PGHOST/DATABASE_URL is set)  ClickHouse pulls straight from Postgres
                    via the postgresql() table function — Postgres is the system of record, no files shipped.
  --from-csv        stream data/cases.csv.gz + data/neighborhoods.csv from this machine.
"""
import csv, os, sys, time
from pathlib import Path
from urllib.parse import urlparse
from clickhouse_connect.driver.tools import insert_file
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ch import client, run_file, ROOT

def pg_conn():
    """(host:port, db, user, password) from DATABASE_URL or PG* vars."""
    if os.getenv("DATABASE_URL"):
        u = urlparse(os.getenv("DATABASE_URL"))
        return f"{u.hostname}:{u.port or 5432}", (u.path or "/postgres").lstrip("/"), u.username, u.password
    if os.getenv("PGHOST"):
        return (f"{os.getenv('PGHOST')}:{os.getenv('PGPORT','5432')}", os.getenv("PGDATABASE", "postgres"),
                os.getenv("PGUSER", "postgres"), os.getenv("PGPASSWORD", ""))
    return None

mode = "csv" if "--from-csv" in sys.argv else "postgres" if (pg_conn() or "--from-postgres" in sys.argv) else "csv"
ch = client(); t0 = time.time()
run_file(ch, ROOT / "sql" / "001_schema.sql")
for t in ["cases", "cases_stage", "nbhd_dim_month", "neighborhoods"]:
    ch.command(f"TRUNCATE TABLE better_days.{t}")

if mode == "postgres":
    hostport, db, user, pw = pg_conn()
    print(f"source: Postgres {hostport}/{db} (ClickHouse postgresql() table function)")
    p = {"h": hostport, "d": db, "u": user, "p": pw}
    ch.command("""INSERT INTO better_days.neighborhoods (id, name, link, residential, centroid_lat, centroid_lon, wkt)
                  SELECT id, name, ifNull(link,''), residential, centroid_lat, centroid_lon, wkt
                  FROM postgresql({h:String}, {d:String}, 'neighborhoods', {u:String}, {p:String})""", parameters=p)
    ch.command("SYSTEM RELOAD DICTIONARY better_days.nbhd_dict")
    ch.command("""INSERT INTO better_days.cases_stage
                  SELECT case_id, opened, closed, ifNull(status,''), ifNull(agency,''), ifNull(category,''), ifNull(request_type,''),
                         ifNull(request_details,''), ifNull(address,''), ifNull(supervisor_district,0), ifNull(neighborhood,''),
                         ifNull(analysis_nbhd,''), ifNull(police_district,''), lat, lon, ifNull(source,'')
                  FROM postgresql({h:String}, {d:String}, 'cases311', {u:String}, {p:String})""", parameters=p)
else:
    print("source: local CSV files")
    rows = []
    with open(ROOT / "data" / "neighborhoods.csv", newline="") as f:
        for r in csv.DictReader(f):
            rows.append([int(r["id"]), r["name"], r["link"], r["residential"] == "1",
                         float(r["centroid_lat"]), float(r["centroid_lon"]), r["wkt"]])
    ch.insert("better_days.neighborhoods", rows, column_names=["id", "name", "link", "residential", "centroid_lat", "centroid_lon", "wkt"])
    ch.command("SYSTEM RELOAD DICTIONARY better_days.nbhd_dict")
    insert_file(ch, "better_days.cases_stage", str(ROOT / "data" / "cases.csv.gz"), fmt="CSVWithNames", compression="gzip",
                settings={"format_csv_null_representation": r"\N", "date_time_input_format": "best_effort"})

print("neighborhoods:", ch.command("SELECT count() FROM better_days.neighborhoods"),
      "| dict test:", ch.command("SELECT dictGet('better_days.nbhd_dict','name',(-122.4270, 37.7596))"))
print("stage rows:", ch.command("SELECT count() FROM better_days.cases_stage"))
run_file(ch, ROOT / "sql" / "002_load.sql")
ch.command("TRUNCATE TABLE better_days.cases_stage")
print("cases rows:", ch.command("SELECT count() FROM better_days.cases"),
      "| blank neighborhood:", ch.command("SELECT count() FROM better_days.cases WHERE neighborhood=''"))
print("by dimension:", ch.query("SELECT dimension, count() c FROM better_days.cases GROUP BY 1 ORDER BY c DESC").result_rows)
print("rollup rows:", ch.command("SELECT count() FROM better_days.nbhd_dim_month"), f"| done in {time.time()-t0:.1f}s")
