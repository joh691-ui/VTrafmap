"""
build_shapes.py - Extrahera sparvagnslinjer fran GTFS-data och spara som JSON.

Anvandning:
    python build_shapes.py API_NYCKEL
    
API_NYCKEL ar din Trafiklab-nyckel for GTFS Sverige 2 (statisk data).
Skriptet laddar ner vt.zip, extraherar shapes.txt, trips.txt och routes.txt,
och sparar en tram_routes.json med koordinater for varje sparvagnslinje.
"""

import sys
import os
import csv
import json
import zipfile
import io

try:
    import requests
except ImportError:
    print("Installerar requests...")
    os.system(f"{sys.executable} -m pip install requests")
    import requests


# GTFS route_type for sparrvagn (VT anvander extended types)
TRAM_ROUTE_TYPES = {0, 900}  # Standard + Extended

# Kanda sparvagnslinjer i Goteborg
TRAM_LINE_NAMES = {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "13"}

# Goteborg tram colors (VT standard)
TRAM_COLORS = {
    "1": "#00A5E3",
    "2": "#F0E442",
    "3": "#00A651",
    "4": "#00A651",
    "5": "#E4002B",
    "6": "#FF6900",
    "7": "#8B572A",
    "8": "#E4002B",
    "9": "#1F67B1",
    "10": "#006C93",
    "11": "#333333",
    "13": "#8B008B",
}

DOWNLOAD_URLS = [
    "https://opendata.samtrafiken.se/gtfs/vt/vt.zip?key={key}",
    "https://opendata.samtrafiken.se/gtfs-sweden/vt.zip?key={key}",
]


def download_gtfs(api_key):
    """Ladda ner GTFS-data fran Trafiklab (eller anvand lokal cache)."""
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vt.zip")
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 1000:
        print(f"  Anvander cachad vt.zip ({os.path.getsize(cache_path)/1024/1024:.1f} MB)")
        with open(cache_path, "rb") as f:
            return f.read()

    for url_template in DOWNLOAD_URLS:
        url = url_template.format(key=api_key)
        display = url.replace(api_key, api_key[:8] + "...")
        print(f"  Forsoker: {display}")
        try:
            resp = requests.get(url, timeout=120)
            if resp.status_code == 200 and len(resp.content) > 1000:
                print(f"  Nedladdat: {len(resp.content) / 1024 / 1024:.1f} MB")
                with open(cache_path, "wb") as f:
                    f.write(resp.content)
                return resp.content
            print(f"  Status: {resp.status_code}, storlek: {len(resp.content)} bytes")
        except Exception as e:
            print(f"  FEL: {e}")

    print("\n  Kunde inte ladda ner GTFS-data!")
    sys.exit(1)


def extract_shapes(zip_data):
    """Extrahera ruttformer for sparvagnar fran GTFS-data."""
    zf = zipfile.ZipFile(io.BytesIO(zip_data))
    files = zf.namelist()
    print(f"  Filer i arkivet: {', '.join(sorted(files)[:12])}...")

    # 1. Lasa routes.txt -> hitta alla sparvagnsrutter (route_type 900)
    tram_route_ids = {}  # route_id -> {name, color}
    with zf.open("routes.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            route_type = int(row.get("route_type", 3))
            name = row.get("route_short_name", "").strip()
            if route_type in TRAM_ROUTE_TYPES and name in TRAM_LINE_NAMES:
                route_id = row["route_id"]
                color = TRAM_COLORS.get(name, "#0074BF")
                tram_route_ids[route_id] = {"name": name, "color": color}

    print(f"  Hittade {len(tram_route_ids)} sparvagnsrutter")

    # 2. Lasa trips.txt -> hitta shape_id for varje sparvagnslinje
    # Vi vill ha EN shape per linje+riktning (den langsta)
    line_shapes = {}  # (line_name, direction) -> shape_id
    shape_to_line = {}  # shape_id -> line_name
    trips_per_shape = {}  # shape_id -> count

    with zf.open("trips.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            route_id = row.get("route_id", "")
            if route_id not in tram_route_ids:
                continue
            shape_id = row.get("shape_id", "").strip()
            if not shape_id:
                continue
            direction = int(row.get("direction_id", 0))
            line_name = tram_route_ids[route_id]["name"]

            shape_to_line[shape_id] = line_name
            trips_per_shape[shape_id] = trips_per_shape.get(shape_id, 0) + 1
            
            key = (line_name, direction)
            # Valj den shape som anvands mest (troligtvis "hela" linjen)
            if key not in line_shapes:
                line_shapes[key] = shape_id
            else:
                current = line_shapes[key]
                if trips_per_shape.get(shape_id, 0) > trips_per_shape.get(current, 0):
                    line_shapes[key] = shape_id

    wanted_shapes = set(line_shapes.values())
    print(f"  Valda shapes: {len(wanted_shapes)} (for {len(line_shapes)} linje+riktning-par)")

    # 3. Lasa shapes.txt -> hamta koordinater
    shape_points = {}  # shape_id -> [(lat, lon, seq)]
    
    if "shapes.txt" not in files:
        print("  VARNING: shapes.txt saknas i GTFS-data!")
        print("  Kan ej extrahera ruttformer.")
        return {}

    with zf.open("shapes.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            shape_id = row.get("shape_id", "").strip()
            if shape_id not in wanted_shapes:
                continue
            lat = float(row["shape_pt_lat"])
            lon = float(row["shape_pt_lon"])
            seq = int(row.get("shape_pt_sequence", 0))
            if shape_id not in shape_points:
                shape_points[shape_id] = []
            shape_points[shape_id].append((seq, lat, lon))

    print(f"  Laste {len(shape_points)} shapes med koordinater")

    # 4. Bygg slutresultat: line_name -> [koordinater]
    result = {}
    for (line_name, direction), shape_id in line_shapes.items():
        if shape_id not in shape_points:
            continue
        # Sortera efter sequence
        points = sorted(shape_points[shape_id], key=lambda x: x[0])
        coords = [[round(p[1], 6), round(p[2], 6)] for p in points]
        
        color = TRAM_COLORS.get(line_name, "#0074BF")
        key = f"{line_name}_{direction}"
        result[key] = {
            "line": line_name,
            "direction": direction,
            "color": color,
            "coords": coords,
        }

    return result


def main():
    if len(sys.argv) < 2:
        print("Anvandning: python build_shapes.py DIN_API_NYCKEL")
        print("  API-nyckeln behover ha tillgang till GTFS Sverige 2 (statisk data).")
        sys.exit(1)

    api_key = sys.argv[1]
    print("\n  build_shapes.py - Extraherar sparvagnslinjer")
    print(f"  API-nyckel: {api_key[:8]}...")

    # Ladda ner
    print("\n[1/3] Laddar ner GTFS-data...")
    zip_data = download_gtfs(api_key)

    # Extrahera
    print("\n[2/3] Extraherar sparvagnslinjer...")
    routes = extract_shapes(zip_data)

    if not routes:
        print("\n  Inga sparvagnslinjer hittades!")
        sys.exit(1)

    # Spara
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tram_routes.json")
    print(f"\n[3/3] Sparar {len(routes)} rutter till tram_routes.json...")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(routes, f, ensure_ascii=False)

    # Sammanfattning
    lines = sorted(set(r["line"] for r in routes.values()))
    total_points = sum(len(r["coords"]) for r in routes.values())
    file_size = os.path.getsize(out_path) / 1024

    print(f"\n  Klart!")
    print(f"  Linjer: {', '.join(lines)}")
    print(f"  Totalt {total_points} koordinatpunkter")
    print(f"  Filstorlek: {file_size:.0f} KB")
    print(f"  Sparad: {out_path}")


if __name__ == "__main__":
    main()
