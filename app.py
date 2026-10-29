from flask import Flask, request, jsonify, send_from_directory
import urllib.request
import urllib.parse
import ssl
import json
import os
import base64
import re
import time
from datetime import datetime, timedelta, timezone

app = Flask(__name__, static_folder='.', static_url_path='')

BSH_BASE = "https://gdi.bsh.de/ldproxy/rest/services/WaterLevelForecast/collections/waterlevelforecastdata/items/"
PEGEL_BASE = "https://www.pegelonline.wsv.de/webservices/rest-api/v2/stations/"

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

AUFBEWAHRUNG_TAGE = 4  # alte Einträge nach X Tagen aus dem Cache entfernen


# ==========================================================
# Hilfsfunktion: Beliebige Anfrage an die Supabase REST-API
# (PostgREST) senden.
# ==========================================================
def supabase_request(method, path, body=None, params=None, extra_headers=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_KEY sind nicht gesetzt")

    url = f"{SUPABASE_URL}/rest/v1/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def speichere_ereignisse_supabase(station_id, ereignisse):
    """Speichert (upsert) alle übergebenen HW/NW-Ereignisse dauerhaft in Supabase."""
    zeilen = []
    for e in ereignisse:
        ts = e.get("event_timestamp")
        art = e.get("event")
        wert = e.get("forecast_value")
        if wert is None:
            wert = e.get("tidal_prediction_value")
        if ts is None or art is None or wert is None:
            continue
        zeilen.append({
            "station_id": station_id,
            "event_timestamp": ts,
            "event": art,
            "value": wert
        })

    if not zeilen:
        return

    try:
        supabase_request(
            "POST",
            "tide_events",
            body=zeilen,
            params={"on_conflict": "station_id,event_timestamp"},
            extra_headers={"Prefer": "resolution=merge-duplicates"}
        )
    except Exception as e:
        print(f"Supabase-Speicherfehler: {e}")


def lade_ereignisse_supabase(station_id):
    """Lädt alle bekannten HW/NW-Ereignisse einer Station aus Supabase.
    Gibt bei einem Verbindungsfehler None zurück (statt leerer Liste),
    damit wir das von 'wirklich keine Daten vorhanden' unterscheiden können."""
    try:
        ergebnis = supabase_request(
            "GET",
            "tide_events",
            params={
                "station_id": f"eq.{station_id}",
                "order": "event_timestamp.asc"
            }
        )
    except Exception as e:
        print(f"Supabase-Lesefehler: {e}")
        return None  # 👈 None = Fehler, [] = echte leere Antwort

    return [
        {
            "event_timestamp": r["event_timestamp"],
            "event": r["event"],
            "forecast_value": r["value"]
        }
        for r in (ergebnis or [])
    ]


@app.route('/api/tide')
def tide():
    station_id = request.args.get('id')
    if not station_id:
        return jsonify({"error": "Fehlender Parameter 'id'"}), 400

    bsh_url = f"{BSH_BASE}{station_id}/?f=json"
    frisch = None
    bsh_fehler = None

    try:
        req = urllib.request.Request(bsh_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10, context=ssl_context) as response:
            data = response.read()
        frisch = json.loads(data)
    except urllib.error.HTTPError as e:
        bsh_fehler = f"BSH-API Fehler: {e.reason}"
    except Exception as e:
        bsh_fehler = str(e)

    neue_liste = []
    if frisch is not None:
        neue_liste = frisch.get("properties", {}).get("high_water_low_water", [])
        speichere_ereignisse_supabase(station_id, neue_liste)
        raeume_alte_eintraege_auf_supabase(station_id)

    gemergte_liste = lade_ereignisse_supabase(station_id)

    if gemergte_liste is None:
        # Supabase nicht erreichbar -> als Fallback einfach die frischen BSH-Daten nutzen
        print("Supabase nicht erreichbar, nutze frische BSH-Daten ohne Cache-Merge")
        gemergte_liste = neue_liste

    if not gemergte_liste:
        if bsh_fehler:
            return jsonify({"error": bsh_fehler}), 502
        return jsonify({"properties": {"high_water_low_water": []}})

    antwort = {"properties": {"high_water_low_water": gemergte_liste}}
    if frisch is not None:
        antwort["properties"]["forecast_timestamp"] = frisch.get("properties", {}).get("forecast_timestamp")

    return jsonify(antwort)


def raeume_alte_eintraege_auf_supabase(station_id, tage=AUFBEWAHRUNG_TAGE):
    grenze = (datetime.now(timezone.utc) - timedelta(days=tage)).isoformat()
    try:
        supabase_request(
            "DELETE",
            "tide_events",
            params={
                "station_id": f"eq.{station_id}",
                "event_timestamp": f"lt.{grenze}"
            }
        )
    except Exception as e:
        print(f"Supabase-Aufräumfehler: {e}")

# ==========================================================
# Winddaten (Nordwind e.V.)
# ==========================================================
NORDWIND_URL = "https://www.nordwind-ev.de/werte/werteHTML5.php"
NORDWIND_TOKEN = os.environ.get("NORDWIND_TOKEN")
WIND_CACHE_SEKUNDEN = 60
_wind_cache = {}  # station -> (zeitstempel, daten)

KOMPASS = ["N", "NNO", "NO", "ONO", "O", "OSO", "SO", "SSO",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def mph_umrechnen(mph):
    """Rohwert (mph) -> alle gängigen Einheiten."""
    if mph is None:
        return None
    ms = mph * 0.44704
    return {
        "ms": round(ms, 1),
        "kmh": round(mph * 1.609344, 1),
        "kn": round(mph * 0.86897699263671, 1),
        "bft": min(12, round((ms ** 0.694) * 1.102)),
    }


def zahl(werte, key):
    try:
        return float(werte[key])
    except (KeyError, ValueError):
        return None


def parse_nordwind(text):
    """Base64 dekodieren und 'name,wert;name,wert;...' in ein Dict umwandeln."""
    text = text.strip()
    text += "=" * (-len(text) % 4)  # fehlendes Padding auffüllen
    roh = base64.b64decode(text).decode("utf-8", errors="replace")
    werte = {}
    for teil in roh.split(";"):
        if "," in teil:
            k, v = teil.split(",", 1)
            werte[k.strip()] = v.strip()
    return werte


@app.route('/api/wind')
def wind():
    station = request.args.get('station', 'amrum')
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", station):
        return jsonify({"error": "Ungültiger Stationsname"}), 400
    if not NORDWIND_TOKEN:
        return jsonify({"error": "NORDWIND_TOKEN ist nicht gesetzt"}), 503

    jetzt = time.time()
    cached = _wind_cache.get(station)
    if cached and jetzt - cached[0] < WIND_CACHE_SEKUNDEN:
        return jsonify(cached[1])

    query = urllib.parse.urlencode({"station": station, "s": NORDWIND_TOKEN})
    try:
        req = urllib.request.Request(f"{NORDWIND_URL}?{query}",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            text = resp.read().decode("ascii", errors="ignore")
        if text.startswith("relo"):
            raise RuntimeError("Nordwind: Token ungültig oder abgelaufen")
        w = parse_nordwind(text)

        wind_mph = zahl(w, "windspeed")
        if wind_mph is not None and wind_mph > 125:   # Ausreißer wie im Original
            wind_mph = None
        grad = zahl(w, "windrichtung")
        zeit = zahl(w, "time")

        daten = {
            "station": station,
            "zeit": datetime.fromtimestamp(zeit, timezone.utc).isoformat() if zeit else None,
            "richtung": None if grad is None else {
                "grad": round(grad),
                "kompass": KOMPASS[round((grad % 360) / 22.5) % 16],
            },
            "wind": mph_umrechnen(wind_mph),
            "boe": mph_umrechnen(zahl(w, "windboe")),
            "mittel10min": mph_umrechnen(zahl(w, "windspeed_avg_10min")),
            "maxHeute": mph_umrechnen(zahl(w, "windspeed_max_day")),
        }
        _wind_cache[station] = (jetzt, daten)
        return jsonify(daten)

    except Exception as e:
        print(f"Wind-Fehler: {e}")
        if cached:  # lieber alte Daten als gar keine
            return jsonify({**cached[1], "veraltet": True})
        return jsonify({"error": str(e)}), 502
        
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/api/pegel')
def pegel():
    uuid = request.args.get('uuid')
    mode = request.args.get('mode', 'info')
    tage = request.args.get('tage', '3')
    reihe = request.args.get('reihe', 'W')  # 👈 NEU: welche Zeitreihe (W oder WT)

    if not uuid:
        return jsonify({"error": "Fehlender Parameter 'uuid'"}), 400

    if mode == 'messreihe':
        pegel_url = f"{PEGEL_BASE}{uuid}/{reihe}/measurements.json?start=P{tage}D"
    elif mode == 'kennwerte':
        pegel_url = f"{PEGEL_BASE}{uuid}/{reihe}/characteristicvalues.json"
    else:
        pegel_url = f"{PEGEL_BASE}{uuid}.json?includeTimeseries=true&includeCurrentMeasurement=true"

    try:
        req = urllib.request.Request(pegel_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10, context=ssl_context) as response:
            data = response.read()
        return app.response_class(data, mimetype='application/json')
    except urllib.error.HTTPError as e:
        return jsonify({"error": f"Pegelonline-API Fehler: {e.reason}"}), e.code
    except Exception as e:
        return jsonify({"error": str(e)}), 500



if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
