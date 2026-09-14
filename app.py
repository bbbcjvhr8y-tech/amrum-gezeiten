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

ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

# ==========================================================
# Cache-Datei, in der wir alle jemals gesehenen HW/NW-Ereignisse
# je Station dauerhaft speichern. So bleiben auch bereits
# vergangene Tiden des heutigen Tages sichtbar, obwohl die
# BSH-API selbst nur noch zukünftige Tiden liefert.
# ==========================================================
CACHE_DATEI = "tide_cache.json"
AUFBEWAHRUNG_TAGE = 4  # alte Einträge nach X Tagen aus dem Cache entfernen


def lade_cache():
    if not os.path.exists(CACHE_DATEI):
        return {}
    try:
        with open(CACHE_DATEI, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Cache konnte nicht gelesen werden: {e}")
        return {}


def speichere_cache(cache):
    try:
        with open(CACHE_DATEI, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"Cache konnte nicht gespeichert werden: {e}")


def raeume_alte_eintraege_auf(eintraege):
    grenze = datetime.now(timezone.utc) - timedelta(days=AUFBEWAHRUNG_TAGE)
    ergebnis = []
    for e in eintraege:
        try:
            zeit = datetime.fromisoformat(e["event_timestamp"].replace("Z", "+00:00"))
            if zeit >= grenze:
                ergebnis.append(e)
        except Exception:
            ergebnis.append(e)  # im Zweifel behalten
    return ergebnis


def merge_ereignisse(alt, neu):
    """Merged zwei Listen von HW/NW-Ereignissen anhand des Zeitstempels,
    neue Werte überschreiben alte (falls BSH eine Vorhersage aktualisiert)."""
    nach_zeit = {e["event_timestamp"]: e for e in alt}
    for e in neu:
        nach_zeit[e["event_timestamp"]] = e
    ergebnis = list(nach_zeit.values())
    ergebnis.sort(key=lambda e: e["event_timestamp"])
    return ergebnis


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/api/tide')
def tide():
    station_id = request.args.get('id')
    if not station_id:
        return jsonify({"error": "Fehlender Parameter 'id'"}), 400

    bsh_url = f"{BSH_BASE}{station_id}/?f=json"

    try:
        req = urllib.request.Request(bsh_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10, context=ssl_context) as response:
            data = response.read()
        frisch = json.loads(data)
    except urllib.error.HTTPError as e:
        # Wenn BSH gerade nicht erreichbar ist, versuchen wir trotzdem,
        # aus dem Cache zu antworten, damit die Seite nicht leer bleibt.
        frisch = None
        bsh_fehler = f"BSH-API Fehler: {e.reason}"
    except Exception as e:
        frisch = None
        bsh_fehler = str(e)

    cache = lade_cache()
    alte_liste = cache.get(station_id, [])

    neue_liste = []
    if frisch is not None:
        neue_liste = frisch.get("properties", {}).get("high_water_low_water", [])

    gemergte_liste = merge_ereignisse(alte_liste, neue_liste)
    gemergte_liste = raeume_alte_eintraege_auf(gemergte_liste)

    cache[station_id] = gemergte_liste
    speichere_cache(cache)

    if frisch is not None:
        antwort = frisch
        antwort["properties"]["high_water_low_water"] = gemergte_liste
    else:
        if not gemergte_liste:
            return jsonify({"error": bsh_fehler}), 502
        # Fallback-Antwort nur aus dem Cache, wenn BSH nicht erreichbar war
        antwort = {"properties": {"high_water_low_water": gemergte_liste}}

    return jsonify(antwort)


@app.route('/api/pegel')
def pegel():
    """
    Proxy für PEGELONLINE (Live-Wasserstand + Wassertemperatur),
    damit der Browser keine direkten Cross-Origin-Anfragen stellen muss.

    Query-Parameter:
      - uuid: Stations-UUID (Pflicht)
      - mode: 'info' (Stationsinfo + currentMeasurement, Default)
              oder 'messreihe' (Zeitreihe der letzten Tage)
      - tage: nur bei mode=messreihe, Anzahl Tage zurück (Default 3)
    """
    uuid = request.args.get('uuid')
    mode = request.args.get('mode', 'info')
    tage = request.args.get('tage', '3')

    if not uuid:
        return jsonify({"error": "Fehlender Parameter 'uuid'"}), 400

    if mode == 'messreihe':
        pegel_url = f"{PEGEL_BASE}{uuid}/W/measurements.json?start=P{tage}D"
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
