"""Smart wallet - laptop app.

Reads the Nicla's predictions over USB, keeps track of the bill stack and the
balance (always in USD), and serves a live dashboard at http://127.0.0.1:5050

Run:
    python app.py                 # auto-detect the Nicla's serial port
    python app.py --port /dev/tty.usbmodem1234   (or COM5 on Windows)
    python app.py --simulate      # no camera; use the buttons on the dashboard
"""

import argparse
import json
import math
import os
import queue
import threading
import time
import uuid
from datetime import datetime

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

from wallet import NONE, Debouncer, Wallet

# ---------------------------------------------------------------- settings ---
GOOGLE_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
SEARCH_RADIUS_M = 10.0          # vendors within ~10 m
FALLBACK_RADIUS_M = 50.0        # used only if nothing is found within 10 m
MAX_VENDORS = 8
MOVE_THRESHOLD_M = 75           # ignore location jitter smaller than this

# Face value of the ariary note used in the demo (a 20,000 Ar bill).
ARIARY_NOTE_VALUE = 20000.0
# Live USD->MGA rate. Fetched fresh for every ariary transaction - never a
# hard-coded number. If the first source fails, the second is tried; if both
# fail, the transaction waits and retries until a live rate arrives.
RATE_SOURCES = [
    ("ExchangeRate-API", "https://open.er-api.com/v6/latest/USD"),
    ("fawazahmed0 currency-api", "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json"),
]
RATE_RETRY_SECONDS = 3

USD_VALUE = {"1": 1.0, "20": 20.0}

STABLE_FRAMES = 6                # ~0.6 s at 10 fps
MIN_CONFIDENCE = 0.70

# If your labels.txt uses other names, map them here.
LABEL_MAP = {
    "none": "none", "empty": "none", "background": "none",
    "1": "1", "one": "1", "1usd": "1", "usd1": "1", "1$": "1", "$1": "1",
    "20": "20", "twenty": "20", "20usd": "20", "usd20": "20", "20$": "20", "$20": "20",
    "ariary": "ariary", "mga": "ariary", "20000ariary": "ariary", "20000": "ariary",
}

# ------------------------------------------------------------------- state ---
lock = threading.RLock()
wallet = Wallet()
debouncer = Debouncer(STABLE_FRAMES, MIN_CONFIDENCE)
state = {
    "balance_usd": 0.0,
    "transactions": [],          # newest last
    "camera": {"connected": False, "port": None, "label": None, "conf": None,
               "stable": None, "simulate": False},
    "location": None,            # {lat, lng, accuracy, at}
    "rate": None,                # latest live rate, see get_live_rate()
    "last_event": None,          # short message shown on the dashboard
}
subscribers = []

app = Flask(__name__, static_folder="static")


def snapshot():
    with lock:
        return {
            "balance_usd": state["balance_usd"],
            "stack": list(wallet.stack),
            "transactions": list(reversed(state["transactions"])),
            "camera": dict(state["camera"]),
            "location": state["location"],
            "rate": state["rate"],
            "last_event": state["last_event"],
            "config": {"ariary_note_value": ARIARY_NOTE_VALUE,
                       "places_enabled": bool(GOOGLE_API_KEY),
                       "radius_m": SEARCH_RADIUS_M},
        }


def broadcast():
    data = json.dumps(snapshot())
    for q in list(subscribers):
        try:
            q.put_nowait(data)
        except queue.Full:
            pass


# -------------------------------------------------------- external services ---
def get_live_rate():
    """Fetch the current USD->MGA rate from a live source.

    Returns {"mga_per_usd", "usd_per_mga", "source", "source_updated", "fetched_at"}
    or None if no source could be reached right now.
    """
    for name, url in RATE_SOURCES:
        try:
            resp = requests.get(url, timeout=6)
            resp.raise_for_status()
            d = resp.json()
            if "rates" in d:                          # ExchangeRate-API
                if d.get("result") != "success":
                    raise ValueError(d.get("error-type", "bad result"))
                mga, updated = float(d["rates"]["MGA"]), d.get("time_last_update_utc")
            else:                                     # currency-api
                mga, updated = float(d["usd"]["mga"]), d.get("date")
            if not 1000 < mga < 20000:                # sanity check
                raise ValueError(f"implausible rate {mga}")
            rate = {"mga_per_usd": mga, "usd_per_mga": 1 / mga, "source": name,
                    "source_updated": updated, "fetched_at": now_iso()}
            with lock:
                state["rate"] = rate
            return rate
        except Exception as e:
            print(f"[rate] {name} failed: {e}")
    return None


def rate_refresher():
    """Keeps the rate pill on the dashboard current."""
    while True:
        if get_live_rate():
            broadcast()
            time.sleep(300)
        else:
            time.sleep(RATE_RETRY_SECONDS)


def distance_m(lat1, lng1, lat2, lng2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearby_vendors(lat, lng):
    """Google Places API (New) Nearby Search. Returns (vendors, radius_used, note)."""
    if not GOOGLE_API_KEY:
        return [], None, "No GOOGLE_PLACES_API_KEY set"
    url = "https://places.googleapis.com/v1/places:searchNearby"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_API_KEY,
        "X-Goog-FieldMask": "places.id,places.displayName,places.primaryTypeDisplayName,"
                            "places.formattedAddress,places.location",
    }
    note = None
    for radius in (SEARCH_RADIUS_M, FALLBACK_RADIUS_M):
        body = {
            "maxResultCount": 20,
            "rankPreference": "DISTANCE",
            "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng},
                                               "radius": radius}},
        }
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=8)
            if resp.status_code != 200:
                return [], radius, f"Places error {resp.status_code}: {resp.text[:200]}"
            places = resp.json().get("places", [])
        except Exception as e:
            return [], radius, f"Places request failed: {e}"
        if places:
            vendors = []
            for p in places:
                loc = p.get("location", {})
                vendors.append({
                    "name": p.get("displayName", {}).get("text", "Unknown"),
                    "type": p.get("primaryTypeDisplayName", {}).get("text"),
                    "address": p.get("formattedAddress"),
                    "distance_m": round(distance_m(lat, lng, loc.get("latitude", lat),
                                                   loc.get("longitude", lng))),
                })
            vendors.sort(key=lambda v: v["distance_m"])
            if radius != SEARCH_RADIUS_M:
                note = f"Nothing within {SEARCH_RADIUS_M:.0f} m; widened to {radius:.0f} m"
            return vendors[:MAX_VENDORS], radius, note
    return [], FALLBACK_RADIUS_M, f"No places within {FALLBACK_RADIUS_M:.0f} m"


_name_cache = {}   # (lat, lng) rounded to ~10 m  ->  address string


def _short_google(result):
    """'123 Main St, Boston, MA 02110, USA' -> '123 Main St, Boston'"""
    comps = {t: c for c in result.get("address_components", []) for t in c["types"]}
    street = " ".join(x for x in (comps.get("street_number", {}).get("long_name"),
                                  comps.get("route", {}).get("long_name")) if x)
    city = (comps.get("locality") or comps.get("postal_town") or
            comps.get("sublocality") or comps.get("administrative_area_level_2") or {}).get("long_name")
    poi = (comps.get("point_of_interest") or comps.get("establishment") or {}).get("long_name")
    parts = [p for p in (poi, street, city) if p]
    return ", ".join(dict.fromkeys(parts)) or result.get("formatted_address")


def _short_osm(d):
    a = d.get("address", {})
    street = " ".join(x for x in (a.get("house_number"), a.get("road")) if x)
    city = a.get("city") or a.get("town") or a.get("village") or a.get("suburb") or a.get("county")
    poi = d.get("name") or None
    parts = [p for p in (poi, street, city) if p]
    return ", ".join(dict.fromkeys(parts)) or d.get("display_name")


def place_name(lat, lng):
    """Human-readable address for a coordinate, or None if every service failed.

    1. Google Geocoding API (same key; 'Geocoding API' must be enabled)
    2. OpenStreetMap Nominatim (free, no key) as a backup
    Results are cached per ~10 m so repeat lookups are instant.
    """
    key = (round(lat, 4), round(lng, 4))
    if key in _name_cache:
        return _name_cache[key]
    name = None
    if GOOGLE_API_KEY:
        try:
            r = requests.get("https://maps.googleapis.com/maps/api/geocode/json",
                             params={"latlng": f"{lat},{lng}", "key": GOOGLE_API_KEY},
                             timeout=6).json()
            if r.get("status") == "OK" and r.get("results"):
                name = _short_google(r["results"][0])
            else:
                print(f"[geocode] Google: {r.get('status')} {r.get('error_message', '')}")
        except Exception as e:
            print(f"[geocode] Google failed: {e}")
    if not name:
        try:
            r = requests.get("https://nominatim.openstreetmap.org/reverse",
                             params={"lat": lat, "lon": lng, "format": "jsonv2", "zoom": 18},
                             headers={"User-Agent": "SmartWalletDemo/1.0"}, timeout=6).json()
            if "error" not in r:
                name = _short_osm(r)
        except Exception as e:
            print(f"[geocode] OpenStreetMap failed: {e}")
    if name:
        _name_cache[key] = name
    return name


def name_current_location(lat, lng):
    """Background: label the laptop's current position on the dashboard."""
    name = place_name(lat, lng)
    with lock:
        loc = state["location"]
        if loc and loc["lat"] == lat and loc["lng"] == lng:
            loc["name"] = name
            loc["name_status"] = "found" if name else "failed"
    broadcast()


# ------------------------------------------------------------ transactions ---
def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def handle_state(new_state):
    """Called with each clean state change from the camera (or demo buttons)."""
    with lock:
        state["camera"]["stable"] = new_state
        result = wallet.on_state(new_state)
        if result is None:
            broadcast()
            return
    direction, notes = result

    parts = [f"{ARIARY_NOTE_VALUE:,.0f} Ar" if n == "ariary" else f"${USD_VALUE[n]:,.0f}"
             for n in notes]
    original = " + ".join(parts)
    note = notes[0] if len(notes) == 1 else "multiple"
    has_ariary = "ariary" in notes
    rate = get_live_rate() if has_ariary else None   # fresh rate for this transaction

    with lock:
        loc = dict(state["location"]) if state["location"] else None
        tx = {
            "id": uuid.uuid4().hex[:8],
            "direction": direction,
            "note": note,
            "original": original,
            "notes": notes,
            "amount_usd": None,
            "er": None,                       # only set for ariary transactions
            "er_status": "waiting for live rate" if has_ariary else None,
            "time": now_iso(),
            "location": loc,
            "counterparty_options": [],
            "counterparty": None,
            "counterparty_status": "searching" if loc else "no location",
            "counterparty_note": None,
            "balance_after": None,
        }
        state["transactions"].append(tx)
        if not has_ariary or rate:
            apply_amount(tx, rate)
        if len(notes) > 1:
            state["last_event"] = f"Emptied the wallet: {original}"
        else:
            state["last_event"] = f"{'Added' if direction == 'in' else 'Removed'} {original}"
        print(f"[tx] {direction:3} {original:>10}  balance ${state['balance_usd']:.2f}  "
              f"stack {wallet.stack}")
    broadcast()

    if has_ariary and not rate:   # no live source reachable: retry until one is
        threading.Thread(target=wait_for_rate, args=(tx["id"],), daemon=True).start()

    if loc:  # lookups can take a second; don't block the dashboard
        threading.Thread(target=fill_counterparties, args=(tx["id"], loc), daemon=True).start()


def apply_amount(tx, rate):
    """Price the transaction in USD and update the balance. Call with lock held."""
    total = 0.0
    for n in tx["notes"]:
        total += ARIARY_NOTE_VALUE * rate["usd_per_mga"] if n == "ariary" else USD_VALUE[n]
    signed = total if tx["direction"] == "in" else -total
    tx["amount_usd"] = round(signed, 6)
    if rate:
        tx["er"] = {"mga_per_usd": rate["mga_per_usd"], "source": rate["source"],
                    "source_updated": rate["source_updated"], "fetched_at": rate["fetched_at"]}
        tx["er_status"] = None
    state["balance_usd"] = round(state["balance_usd"] + signed, 6)
    pending = any(t["amount_usd"] is None for t in state["transactions"])
    if (not wallet.stack and not pending) or abs(state["balance_usd"]) < 0.005:
        state["balance_usd"] = 0.0          # empty wallet = exactly $0 (no rounding leftovers)
    tx["balance_after"] = state["balance_usd"]


def wait_for_rate(tx_id):
    while True:
        time.sleep(RATE_RETRY_SECONDS)
        rate = get_live_rate()
        if rate:
            with lock:
                for tx in state["transactions"]:
                    if tx["id"] == tx_id and tx["amount_usd"] is None:
                        apply_amount(tx, rate)
            broadcast()
            return


def fill_counterparties(tx_id, loc):
    if not loc.get("name"):
        name = place_name(loc["lat"], loc["lng"])
        with lock:
            for tx in state["transactions"]:
                if tx["id"] == tx_id:
                    tx["location"]["name"] = name
                    tx["location"]["name_status"] = "found" if name else "failed"
        broadcast()
    vendors, radius, note = nearby_vendors(loc["lat"], loc["lng"])
    with lock:
        for tx in state["transactions"]:
            if tx["id"] == tx_id:
                tx["counterparty_options"] = vendors
                tx["counterparty_status"] = "found" if vendors else "none found"
                tx["counterparty_note"] = note
                if vendors and not tx["counterparty"]:
                    tx["counterparty"] = vendors[0]["name"]   # closest one preselected
    broadcast()


# ------------------------------------------------------------ serial input ---
def find_port():
    from serial.tools import list_ports
    ports = list(list_ports.comports())
    for p in ports:
        text = f"{p.description} {p.manufacturer} {p.product}".lower()
        if any(k in text for k in ("openmv", "nicla", "arduino")):
            return p.device
    for p in ports:
        if "usbmodem" in p.device or "ttyACM" in p.device:
            return p.device
    return None


def serial_loop(port_arg):
    import serial
    while True:
        port = port_arg or find_port()
        if not port:
            with lock:
                state["camera"].update(connected=False, port=None)
            broadcast()
            time.sleep(2)
            continue
        try:
            with serial.Serial(port, 115200, timeout=1) as ser:
                print(f"[serial] connected to {port}")
                with lock:
                    state["camera"].update(connected=True, port=port)
                broadcast()
                last_label = None
                while True:
                    raw = ser.readline()
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw.decode(errors="ignore").strip())
                    except ValueError:
                        continue          # boot text or partial line
                    raw_label = str(msg.get("label", "")).strip().lower()
                    label = LABEL_MAP.get(raw_label)
                    if label is None and ("ariary" in raw_label or "mga" in raw_label):
                        label = "ariary"          # e.g. "20000ariary", "20000_ariary"
                    conf = float(msg.get("conf", 0))
                    if label is None:
                        continue
                    with lock:
                        state["camera"].update(label=label, conf=conf)
                        changed = debouncer.feed(label, conf)
                    if changed is not None:
                        handle_state(changed)
                    elif label != last_label:
                        broadcast()       # live label on the dashboard
                    last_label = label
        except (serial.SerialException, OSError) as e:
            print(f"[serial] {port}: {e} - retrying")
            with lock:
                state["camera"].update(connected=False)
            broadcast()
            time.sleep(2)


# -------------------------------------------------------------------- web ---
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/state")
def api_state():
    return jsonify(snapshot())


@app.route("/api/stream")
def api_stream():
    q = queue.Queue(maxsize=50)
    subscribers.append(q)

    def gen():
        try:
            yield f"data: {json.dumps(snapshot())}\n\n"
            while True:
                try:
                    yield f"data: {q.get(timeout=15)}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            subscribers.remove(q)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/location", methods=["POST"])
def api_location():
    d = request.get_json(force=True)
    with lock:
        lat, lng = float(d["lat"]), float(d["lng"])
        acc = float(d.get("accuracy") or 999)
        cur = state["location"]
        if cur:
            # Laptop positions (from Wi-Fi) jitter by tens of metres even when
            # standing still. Keep the current fix unless the new one is clearly
            # more accurate, or so far away that we must really have moved.
            moved = distance_m(cur["lat"], cur["lng"], lat, lng)
            more_accurate = acc < (cur["accuracy"] or 999) * 0.8
            really_moved = moved > max(MOVE_THRESHOLD_M, acc, cur["accuracy"] or 0)
            if not (more_accurate or really_moved):
                return jsonify(ok=True, kept=True)
        cached = _name_cache.get((round(lat, 4), round(lng, 4)))
        state["location"] = {"lat": lat, "lng": lng, "accuracy": acc,
                             "at": now_iso(), "name": cached,
                             "name_status": "found" if cached else "searching"}
    if not cached:
        threading.Thread(target=name_current_location, args=(lat, lng), daemon=True).start()
    broadcast()
    return jsonify(ok=True)


@app.route("/api/transactions/<tx_id>/counterparty", methods=["POST"])
def api_counterparty(tx_id):
    name = request.get_json(force=True).get("name")
    with lock:
        for tx in state["transactions"]:
            if tx["id"] == tx_id:
                tx["counterparty"] = name
    broadcast()
    return jsonify(ok=True)


@app.route("/api/simulate", methods=["POST"])
def api_simulate():
    label = request.get_json(force=True).get("label")
    if label not in ("none", "1", "20", "ariary"):
        return jsonify(error="bad label"), 400
    with lock:
        debouncer.force(label)
        state["camera"].update(label=label, conf=1.0)
    handle_state(label)
    return jsonify(ok=True)


@app.route("/api/reset", methods=["POST"])
def api_reset():
    with lock:
        wallet.reset()
        state["balance_usd"] = 0.0
        state["transactions"] = []
        state["last_event"] = "Wallet reset"
    broadcast()
    return jsonify(ok=True)


# ------------------------------------------------- audience (demo) dashboard ---
# Same live data, but no camera status, confidence or demo controls, and none of
# the control endpoints (simulate / reset / counterparty) are reachable here.
demo_app = Flask("demo", static_folder=app.static_folder)


@demo_app.route("/")
def demo_index():
    with open(os.path.join(app.static_folder, "index.html"), encoding="utf-8") as f:
        html = f.read().replace("<body>", '<body class="demo">', 1)
    return Response(html, mimetype="text/html")


demo_app.add_url_rule("/api/state", "api_state", api_state)
demo_app.add_url_rule("/api/stream", "api_stream", api_stream)
demo_app.add_url_rule("/api/location", "api_location", api_location, methods=["POST"])


def lan_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None


def run_demo_server(host, port):
    from werkzeug.serving import make_server
    make_server(host, port, demo_app, threaded=True).serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="serial port of the Nicla (auto-detected if omitted)")
    ap.add_argument("--simulate", action="store_true", help="run without the camera")
    ap.add_argument("--http-port", type=int, default=5050)
    ap.add_argument("--demo-port", type=int, default=5051,
                    help="port of the audience dashboard (0 to turn it off)")
    args = ap.parse_args()

    if not GOOGLE_API_KEY:
        print("[places] GOOGLE_PLACES_API_KEY not set - counterparty lookup disabled")
    threading.Thread(target=rate_refresher, daemon=True).start()

    with lock:
        state["camera"]["simulate"] = args.simulate
    if not args.simulate:
        threading.Thread(target=serial_loop, args=(args.port,), daemon=True).start()

    print(f"Dashboard (operator): http://127.0.0.1:{args.http_port}")
    if args.demo_port:
        threading.Thread(target=run_demo_server, args=("0.0.0.0", args.demo_port), daemon=True).start()
        ip = lan_ip()
        print(f"Demo dashboard:       http://127.0.0.1:{args.demo_port}"
              + (f"   (other devices on this Wi-Fi: http://{ip}:{args.demo_port})" if ip else ""))
    app.run(host="127.0.0.1", port=args.http_port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
