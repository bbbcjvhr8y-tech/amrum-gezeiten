from flask import Flask, request, jsonify, send_from_directory
import urllib.request
import urllib.parse
import ssl
import json
import os
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
    """Lädt alle bekannten HW/NW-Ereignisse einer Station aus Supabase,
    im gleichen Format, das das Frontend erwartet."""
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
        return []

    return [
        {
            "event_timestamp": r["event_timestamp"],
            "event": r["event"],
            "forecast_value": r["value"]
        }
        for r in (ergebnis or [])
    ]


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


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


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

    # Frische Ereignisse (falls vorhanden) dauerhaft in Supabase sichern
    if frisch is not None:
        neue_liste = frisch.get("properties", {}).get("high_water_low_water", [])
        speichere_ereignisse_supabase(station_id, neue_liste)
        raeume_alte_eintraege_auf_supabase(station_id)

    # Immer den komplett gemergten Datenbestand aus Supabase lesen
    gemergte_liste = lade_ereignisse_supabase(station_id)

    if not gemergte_liste:
        if bsh_fehler:
            return jsonify({"error": bsh_fehler}), 502
        return jsonify({"properties": {"high_water_low_water": []}})

    antwort = {"properties": {"high_water_low_water": gemergte_liste}}
    if frisch is not None:
        antwort["properties"]["forecast_timestamp"] = frisch.get("properties", {}).get("forecast_timestamp")

    return jsonify(antwort)


@app.route('/api/pegel')
def pegel():
    uuid = request.args.get('uuid')
    mode = request.args.get('mode', 'info')
    tage = request.args.get('tage', '3')

    if not uuid:
        return jsonify({"error": "Fehlender Parameter 'uuid'"}), 400

    if mode == 'messreihe':
        pegel_url = f"{PEGEL_BASE}{uuid}/W/measurements.json?start=P{tage}D"
    elif mode == 'kennwerte':
        pegel_url = f"{PEGEL_BASE}{uuid}/W/characteristicvalues.json"
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
