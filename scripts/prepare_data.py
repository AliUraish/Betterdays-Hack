"""Trim the raw SF 311 export (50 cols, 116 MB) to the 16 columns we use, ISO dates, gzip.
Also emits neighborhoods.csv with a centroid per polygon. Pure stdlib, ~20s."""
import csv, gzip, re, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
csv.field_size_limit(1 << 30)

def parse_dt(s):
    if not s:
        return r"\N"
    try:
        return datetime.strptime(s, "%m/%d/%Y %I:%M:%S %p").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return r"\N"

def trim_cases():
    src = ROOT / "311_Cases.csv"
    dst = ROOT / "data" / "cases.csv.gz"
    cols = ["case_id","opened","closed","status","agency","category","request_type","request_details",
            "address","supervisor_district","neighborhood","analysis_nbhd","police_district",
            "lat","lon","source"]
    n = kept = 0
    with open(src, newline="") as f, gzip.open(dst, "wt", newline="") as g:
        r = csv.DictReader(f); w = csv.writer(g); w.writerow(cols)
        for row in r:
            n += 1
            if not row["Latitude"] or not row["Longitude"] or not row["Opened"]:
                continue
            opened = parse_dt(row["Opened"])
            if opened == r"\N":
                continue
            sd = row["Supervisor District"].strip()
            w.writerow([
                row["CaseID"], opened, parse_dt(row["Closed"]), row["Status"] or "",
                row["Responsible Agency"], row["Category"], row["Request Type"], row["Request Details"],
                row["Address"], int(float(sd)) if sd else 0, row["Neighborhood"], row["Analysis Neighborhood"] or "",
                row["Police District"], row["Latitude"], row["Longitude"], row["Source"],
            ])
            kept += 1
    print(f"cases: read {n}, kept {kept} -> {dst} ({dst.stat().st_size/1e6:.1f} MB)")

NUM = re.compile(r"(-?\d+\.\d+) (-?\d+\.\d+)")
def centroid(wkt):
    # average of the first (outer) ring's vertices — good enough for labels / "near"
    first_ring = wkt.split("),")[0]
    pts = [(float(a), float(b)) for a, b in NUM.findall(first_ring)]
    return sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)

NON_RESIDENTIAL = {"Golden Gate Park","Presidio National Park","McLaren Park","Treasure Island",
                   "Yerba Buena Island","Candlestick Point SRA","Lincoln Park / Ft. Miley",
                   "Aquatic Park / Ft. Mason","India Basin","Northern Waterfront","Produce Market","Apparel City"}

def trim_neighborhoods():
    src = ROOT / "Neighborhoods_from_6ia5_2f8k.csv"
    dst = ROOT / "data" / "neighborhoods.csv"
    with open(src, newline="") as f, open(dst, "w", newline="") as g:
        w = csv.writer(g); w.writerow(["id","name","link","residential","centroid_lat","centroid_lon","wkt"])
        for row in csv.DictReader(f):
            lat, lon = centroid(row["the_geom"])
            w.writerow([row["Feature ID"], row["name"], row["LINK"], 0 if row["name"] in NON_RESIDENTIAL else 1,
                        f"{lat:.6f}", f"{lon:.6f}", row["the_geom"]])
    print(f"neighborhoods -> {dst}")

if __name__ == "__main__":
    trim_cases(); trim_neighborhoods()
