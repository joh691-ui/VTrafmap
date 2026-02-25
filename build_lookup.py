"""
build_lookup.py - Ladda ner statisk GTFS-data fran Trafiklab och bygg trip_lookup.json.

Anvandning:
    python build_lookup.py DIN_STATIC_API_NYCKEL [operator]
    
    operator: sl (default), vt, etc.

Skriptet laddar ner {operator}.zip fran Trafiklab, extraherar trips.txt och routes.txt,
och skapar en uppslagstabell som mappar trip_id -> {line, type, destination, color}.
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

# Fargkarta for tunnelbanans linjer (SL)
METRO_COLORS = {
    "10": "#1F67B1", "11": "#1F67B1",  # Bla linjen
    "13": "#E4002B", "14": "#E4002B",  # Roda linjen
    "17": "#00A651", "18": "#00A651", "19": "#00A651",  # Grona linjen
}

# Pendeltag farger
COMMUTER_COLORS = {
    "35": "#EC619F", "36": "#EC619F",
    "37": "#EC619F", "38": "#EC619F",
    "40": "#EC619F", "41": "#EC619F",
    "42": "#EC619F", "43": "#EC619F",
    "44": "#EC619F", "48": "#EC619F",
}

# Farger per trafikslag
TYPE_COLORS = {
    "tram": "#FF6600",
    "metro": "#FFFFFF",
    "bus": "#E4002B",
    "boat": "#00E5FF",
    "train": "#EC619F",
    "express": "#00A500",
}

# GTFS route_type -> var typ
ROUTE_TYPE_MAP = {
    0: "tram",      # Tram, Streetcar, Light rail
    1: "metro",     # Subway, Metro
    2: "train",     # Rail
    3: "bus",       # Bus
    4: "boat",      # Ferry
    5: "tram",      # Cable tram
    6: "tram",      # Aerial lift
    7: "tram",      # Funicular
    100: "train",   # Railway Service
    101: "train",   # High Speed Rail
    102: "train",   # Long Distance Trains
    103: "train",   # Inter Regional Rail
    106: "train",   # Regional Rail
    109: "train",   # Suburban Railway
    400: "metro",   # Urban Railway
    700: "bus",     # Bus Service
    702: "bus",     # Express Bus Service
    704: "bus",     # Local Bus Service
    712: "bus",     # School Bus
    714: "bus",     # Rail Replacement Bus
    715: "bus",     # Demand and Response Bus
    717: "bus",     # Share Taxi
    900: "tram",    # Tram Service
    1000: "boat",   # Water Transport Service
    1501: "bus",    # Communal Taxi
}

# URL-mallar for nedladdning ('{op}' byts mot operatorkod)
DOWNLOAD_URLS = [
    "https://opendata.samtrafiken.se/gtfs/{op}/{op}.zip?key={key}",
]


def download_gtfs(api_key, operator):
    """Ladda ner {operator}.zip fran Trafiklab."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "TrafMap/1.0",
        "Accept": "*/*",
    })

    for url_template in DOWNLOAD_URLS:
        url = url_template.format(op=operator, key=api_key)
        # Dolt for sakerhet
        display_url = url_template.format(op=operator, key=api_key[:8] + "...")
        print(f"  Provar: {display_url}")
        try:
            resp = session.get(url, timeout=90, stream=True)
            print(f"  Status: {resp.status_code}")
            if resp.status_code == 200:
                data = resp.content
                print(f"  Nedladdat: {len(data) / 1024 / 1024:.1f} MB")
                return data
            else:
                print(f"  Misslyckades (HTTP {resp.status_code}), provar nasta...")
        except Exception as e:
            print(f"  Fel: {e}, provar nasta...")

    print("\n  KUNDE INTE LADDA NER GTFS-DATA.")
    print("  Kontrollera att din API-nyckel har tillgang till 'GTFS Regional' (statisk data).")
    print("  Pa Trafiklab.se -> Ditt projekt -> Lagg till 'GTFS Regional' API-nyckel.")
    print("  (Din nuvarande nyckel kanske bara galler for 'GTFS Regional Realtime'.)")
    sys.exit(1)


def parse_gtfs(zip_data):
    """Extrahera routes.txt och trips.txt fran zip-filen."""
    zf = zipfile.ZipFile(io.BytesIO(zip_data))
    files = zf.namelist()
    print(f"  Filer i arkivet: {', '.join(files[:10])}{'...' if len(files) > 10 else ''}")

    # Lasa routes.txt -> route_id -> {short_name, type}
    routes = {}
    with zf.open("routes.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            route_id = row["route_id"]
            short_name = row.get("route_short_name", "").strip()
            long_name = row.get("route_long_name", "").strip()
            route_type = int(row.get("route_type", 3))
            vehicle_type = ROUTE_TYPE_MAP.get(route_type, "bus")
            routes[route_id] = {
                "short_name": short_name or long_name,
                "long_name": long_name,
                "type": vehicle_type,
                "route_type": route_type,
            }
    print(f"  Laste {len(routes)} rutter fran routes.txt")

    # Lasa trips.txt -> trip_id -> {route_id, headsign, direction}
    trips = {}
    with zf.open("trips.txt") as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))
        for row in reader:
            trip_id = row["trip_id"]
            route_id = row["route_id"]
            headsign = row.get("trip_headsign", "").strip()
            direction = int(row.get("direction_id", 0))
            trips[trip_id] = {
                "route_id": route_id,
                "headsign": headsign,
                "direction": direction,
            }
    print(f"  Laste {len(trips)} turer fran trips.txt")

    return routes, trips


def build_lookup(routes, trips):
    """Bygg den slutgiltiga uppslagstabellen."""
    lookup = {}
    for trip_id, trip in trips.items():
        route = routes.get(trip["route_id"])
        if not route:
            continue

        line = route["short_name"]
        vtype = route["type"]

        # Farg: specifik farg for tunnelbana, pendeltag, annars typ-baserad
        if vtype == "metro" and line in METRO_COLORS:
            color = METRO_COLORS[line]
        elif vtype == "train" and line in COMMUTER_COLORS:
            color = COMMUTER_COLORS[line]
        elif line.startswith("X") and vtype == "bus":
            color = TYPE_COLORS["express"]
        else:
            color = TYPE_COLORS.get(vtype, TYPE_COLORS["bus"])

        lookup[trip_id] = {
            "line": line,
            "type": vtype,
            "destination": trip["headsign"],
            "color": color,
        }

    return lookup


def main():
    if len(sys.argv) < 2:
        print("Anvandning: python build_lookup.py DIN_API_NYCKEL [operator]")
        print("  operator: sl (default), vt, ul, etc.")
        sys.exit(1)

    api_key = sys.argv[1]
    operator = sys.argv[2] if len(sys.argv) >= 3 else "sl"

    # Ladda ner GTFS-data
    print(f"Laddar ner GTFS Static for '{operator}' fran Trafiklab...")
    zip_data = download_gtfs(api_key, operator)

    # Parsa
    routes, trips = parse_gtfs(zip_data)

    # Bygg lookup
    lookup = build_lookup(routes, trips)
    print(f"\n  Byggde uppslagstabell med {len(lookup)} turer")

    # Statistik per trafikslag
    type_counts = {}
    for v in lookup.values():
        t = v["type"]
        type_counts[t] = type_counts.get(t, 0) + 1
    for t, c in sorted(type_counts.items()):
        print(f"    {t}: {c} turer")

    # Spara
    output_path = os.path.join(os.path.dirname(__file__), "trip_lookup.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(lookup, f, ensure_ascii=False, separators=(",", ":"))
    
    size_mb = os.path.getsize(output_path) / 1024 / 1024
    print(f"\n  Sparade {output_path} ({size_mb:.1f} MB)")
    print("  Klart! Starta nu servern med: python app.py DIN_API_NYCKEL")


if __name__ == "__main__":
    main()
