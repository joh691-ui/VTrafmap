"""
Vasttrafik Live Map - Flask-server med riktig realtidsdata fran Vasttrafiks API.

Anvandning:
    python app.py

Kraver: pip install flask requests
OAuth2-uppgifter konfigureras nedan (CLIENT_ID / CLIENT_SECRET).
"""

from flask import Flask, render_template, jsonify
import json
import os
import sys
import time
import threading
import requests as http_requests

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Ladda sparvagnslinjer (fran build_shapes.py)
# ---------------------------------------------------------------------------
_tram_routes = []
_routes_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tram_routes.json")
if os.path.exists(_routes_file):
    with open(_routes_file, "r", encoding="utf-8") as f:
        _raw = json.load(f)
        _tram_routes = list(_raw.values())
    print(f"  Laddade {len(_tram_routes)} sparvagnsrutter fran tram_routes.json", flush=True)
else:
    print("  VARNING: tram_routes.json saknas - inga linjer pa kartan", flush=True)
    print("  Kor: python build_shapes.py DIN_API_NYCKEL", flush=True)

# ---------------------------------------------------------------------------
# Konfiguration - Vasttrafik Planera Resa v4
# ---------------------------------------------------------------------------

CLIENT_ID = os.environ.get("VT_CLIENT_ID", "WpXrIsZrwdgG9bSC1pj5PNYfrega")
CLIENT_SECRET = os.environ.get("VT_CLIENT_SECRET", "TkbGv1zqYIErrutV8D_1Bg3o2Kwa")

TOKEN_URL = "https://ext-api.vasttrafik.se/token"
POSITIONS_URL = "https://ext-api.vasttrafik.se/pr/v4/positions"
POLL_INTERVAL = 2  # sekunder

# Goteborgsomradet (bounding box for /positions, utokad for att fa med farjor)
GBG_LOWER_LAT = 57.55
GBG_LOWER_LON = 11.70
GBG_UPPER_LAT = 57.90
GBG_UPPER_LON = 12.25
GRID_SIZE = 4  # 4x4 = 16 rutor

# Fallback-farger per transporttyp
FALLBACK_COLORS = {
    "tram": "#0074BF",
    "bus": "#E4002B",
    "train": "#A855F7",
    "ferry": "#00E5FF",
    "unknown": "#0074BF",
}

# Mappning fran API:ets transportMode till var typ
TRANSPORT_MODE_MAP = {
    "tram": "tram",
    "bus": "bus",
    "train": "train",
    "ferry": "boat",
    "ship": "boat",
    "taxi": "bus",
    "unknown": "tram",  # SL:s trams rapporteras som "unknown"
    "none": "bus",
}

# ---------------------------------------------------------------------------
# Globalt tillstand (tradsaker)
# ---------------------------------------------------------------------------

_cached_vehicles = []
_cache_lock = threading.Lock()
_last_fetch_time = 0
_last_fetch_count = 0

# OAuth2 token
_access_token = None
_token_expires = 0
_token_lock = threading.Lock()


# ---------------------------------------------------------------------------
# OAuth2 autentisering
# ---------------------------------------------------------------------------

def get_access_token():
    """Hamta eller fornya OAuth2 access token."""
    global _access_token, _token_expires

    with _token_lock:
        # Ateranvand befintlig token om den fortfarande ar giltig
        if _access_token and time.time() < _token_expires - 60:
            return _access_token

        try:
            resp = http_requests.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(CLIENT_ID, CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            _access_token = data["access_token"]
            _token_expires = time.time() + data.get("expires_in", 3600)
            print(f"  [Auth] Ny token hamtad: {_access_token[:10]}...", flush=True)
            return _access_token
        except Exception as e:
            print(f"  [Auth] FEL vid tokenhamtning: {e}", flush=True)
            return _access_token  # Ateranvand gammal token om mojligt


# ---------------------------------------------------------------------------
# Hamta fordonspositioner fran Vasttrafik
# ---------------------------------------------------------------------------

def fetch_positions():
    """Hamta fordonspositioner fran Vasttrafiks Planera Resa v4 API."""
    global _cached_vehicles, _last_fetch_time, _last_fetch_count

    token = get_access_token()
    if not token:
        print("  FEL: Ingen giltig access token!")
        return

    # Dela upp i GRID_SIZE x GRID_SIZE rutnat for att kringga 100-fordons-gransen
    lat_step = (GBG_UPPER_LAT - GBG_LOWER_LAT) / GRID_SIZE
    lon_step = (GBG_UPPER_LON - GBG_LOWER_LON) / GRID_SIZE
    
    all_vehicles = {}  # detailsReference -> vehicle dict
    errors = 0

    def fetch_cell(row, col):
        """Hamta fordon for en ruta i rutnatet, med retry vid rate-limit."""
        lat1 = GBG_LOWER_LAT + row * lat_step
        lon1 = GBG_LOWER_LON + col * lon_step
        lat2 = lat1 + lat_step
        lon2 = lon1 + lon_step
        for attempt in range(3):
            try:
                current_token = get_access_token()
                resp = http_requests.get(
                    POSITIONS_URL,
                    params={
                        "lowerLeftLat": round(lat1, 6),
                        "lowerLeftLong": round(lon1, 6),
                        "upperRightLat": round(lat2, 6),
                        "upperRightLong": round(lon2, 6),
                        "limit": 200,
                    },
                    headers={
                        "Authorization": f"Bearer {current_token}",
                        "Accept": "application/json",
                    },
                    timeout=10,
                )
                if resp.status_code == 429:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception:
                if attempt < 2:
                    time.sleep(0.3 * (attempt + 1))
        return []

    # Kor rutor med begransad parallellism (4 at gangen)
    from concurrent.futures import ThreadPoolExecutor, as_completed
    cells = [(r, c) for r in range(GRID_SIZE) for c in range(GRID_SIZE)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_cell, r, c): (r, c) for r, c in cells}
        for future in as_completed(futures):
            vehicles = future.result()
            for veh in vehicles:
                ref = veh.get("detailsReference", "")
                if ref and ref not in all_vehicles:
                    all_vehicles[ref] = veh
    
    raw_vehicles = list(all_vehicles.values())

    processed = []
    for veh in raw_vehicles:
        lat = veh.get("latitude")
        lon = veh.get("longitude")
        if lat is None or lon is None:
            continue

        line_info = veh.get("line", {})
        line_name = line_info.get("name", "")
        if not line_name:
            continue

        transport_mode = line_info.get("transportMode", "bus")
        vtype = TRANSPORT_MODE_MAP.get(transport_mode, "bus")
        color = line_info.get("backgroundColor", FALLBACK_COLORS.get(vtype, "#E4002B"))
        fg_color = line_info.get("foregroundColor", "#ffffff")

        # Destination
        direction_details = veh.get("directionDetails", {})
        destination = direction_details.get("shortDirection", "")
        if not destination:
            destination = veh.get("direction", "")

        # Skapa unikt ID
        details_ref = veh.get("detailsReference", "")
        vehicle_id = details_ref if details_ref else f"{line_name}_{lat}_{lon}"

        processed.append({
            "id": vehicle_id,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "line": line_name,
            "type": vtype,
            "color": color,
            "fgColor": fg_color,
            "destination": destination,
            "speed_kmh": 0,  # Ej tillgangligt i detta API
            "bearing": 0,    # Ej tillgangligt i detta API
            "isRealtime": line_info.get("isRealtimeJourney", False),
        })

    with _cache_lock:
        _cached_vehicles = processed
        _last_fetch_time = time.time()
        _last_fetch_count = len(processed)
    
    print(f"  [Fetch] Klart: {len(processed)} fordon uppdaterade", flush=True)


def polling_loop():
    """Bakgrundstrad som hamtar data kontinuerligt."""
    global _access_token
    
    # Hamta forsta token inifran traden sa vi inte blockerar start
    print("  [Thread] Hamtar access token...", flush=True)
    try:
        get_access_token()
    except Exception as e:
        print(f"  [Thread] Kunde inte hamta start-token: {e}", flush=True)

    while True:
        try:
            fetch_positions()
        except Exception as e:
            print(f"  [Thread] Polling misslyckades: {e}", flush=True)
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Flask-routes
# ---------------------------------------------------------------------------

_poller_started = False
_poller_lock = threading.Lock()

def start_poller_if_needed():
    """Sakerstaller att bakgrundstraden startar i rätt process (worker)."""
    global _poller_started
    if not _poller_started:
        with _poller_lock:
            if not _poller_started:
                poller = threading.Thread(target=polling_loop, daemon=True)
                poller.start()
                _poller_started = True
                print("  [Main] Bakgrundstrad startad i worker", flush=True)

@app.before_request
def before_request():
    start_poller_if_needed()

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/vehicles")
def api_vehicles():
    count = 0
    with _cache_lock:
        count = len(_cached_vehicles)
        data = jsonify(_cached_vehicles)
    
    # Logga bara var 5:e anrop for att inte fylla loggen
    if time.time() % 10 < 2:  # Slumpmassig loggning baserat på tid
        print(f"  [API] Serverade {count} fordon", flush=True)
    return data


_weather_cache = None
_weather_cache_time = 0
_WEATHER_CACHE_TTL = 30 * 60  # 30 minuter caching (eftersom prognoser inte uppdateras så ofta)

@app.route("/api/weather")
def api_weather():
    global _weather_cache, _weather_cache_time
    
    now = time.time()
    if _weather_cache and (now - _weather_cache_time < _WEATHER_CACHE_TTL):
        return jsonify(_weather_cache)
        
    try:
        # Hämta prognos från SMHI SNOW1gv1
        lon, lat = 11.97, 57.71
        url = f"https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1/geotype/point/lon/{lon}/lat/{lat}/data.json"
        
        resp = http_requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        
        # Plocka ut första tidsserien (närmast i tid)
        if "timeSeries" in data and len(data["timeSeries"]) > 0:
            current = data["timeSeries"][0]["data"]
            temp = current.get("air_temperature")
            symbol = current.get("symbol_code")
            
            _weather_cache = {
                "temp": temp,
                "symbol": symbol
            }
            _weather_cache_time = now
            return jsonify(_weather_cache)
            
    except Exception as e:
        print(f"  [Weather API] FEL vid hämtning av väder: {e}", flush=True)
        # Returnera gammal cache eller ett felmeddelande
        if _weather_cache:
            return jsonify(_weather_cache)
            
    return jsonify({"error": "Kunde inte hämta väder"}), 500



@app.route("/api/routes")
def api_routes():
    return jsonify(_tram_routes)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

print("\n  Vasttrafik Live Map - App initierad", flush=True)
# Traden startas nu via start_poller_if_needed() vid forsta anrop


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"  Oppna http://127.0.0.1:{port} i din webblasare\n", flush=True)
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
