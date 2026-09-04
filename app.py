from flask import Flask, request, jsonify, send_from_directory
import urllib.request
import urllib.parse
import ssl
import json

app = Flask(__name__, static_folder='.', static_url_path='')

BSH_BASE = "https://gdi.bsh.de/ldproxy/rest/services/WaterLevelForecast/collections/waterlevelforecastdata/items/"

ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

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
        return app.response_class(data, mimetype='application/json')
    except urllib.error.HTTPError as e:
        return jsonify({"error": f"BSH-API Fehler: {e.reason}"}), e.code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
