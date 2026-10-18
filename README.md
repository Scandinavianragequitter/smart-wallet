# Smart Wallet

Hardware project for HackHarvard 2026. 

A wallet prototype that tracks the cash inside it. A Nicla Vision camera
classifies the note on top of the stack; a laptop app turns those readings into
transactions with amount, live exchange rate, time, location and nearby vendors.

```
Nicla Vision  --USB serial (JSON lines)-->  laptop/app.py  --live updates-->  dashboards
nicla/main.py                                stack logic, rates,            operator  :5050
                                             location, vendors              audience  :5051
```

## How it works
- The camera model has four classes: `none`, `1`, `20`, `20000ariary`. (We track 1$, 20$ and 20.000 AriAry)
- A reading only counts after **6 frames in a row** with the same label at ≥70 % confidence.
- The app remembers the stack of notes (bottom → top). On a state change:
  - new state = the note *below* the top → the top note was **taken out**
  - `none` while notes are inside → **everything was taken out at once**
- Balance is always in USD. The 20,000 Ar note is converted with a **live** rate fetched
  for every ariary transaction (ExchangeRate-API, backup: fawazahmed0 currency-api).
  If no live source is reachable the transaction waits instead of using a fixed rate.
- Location comes from the browser (macOS Location Services), filtered against Wi-Fi jitter.
  Address: Google Geocoding API, backup OpenStreetMap Nominatim.
- Counterparties: Google Places API (New) Nearby Search within ~10 m (widens to 50 m if empty).

## 1. Camera (Nicla Vision)
1. From Edge Impulse → Deployment → **OpenMV library**, copy onto the Nicla's drive:
   `ei-model.tflite` → `trained.tflite`, `ei-model.txt` → `labels.txt`.
2. Open `nicla/main.py` in OpenMV IDE, run it, check the serial terminal shows
   lines like `{"label": "20", "conf": 0.93}`.
3. **Tools → Save open script to OpenMV Cam (as main.py)**, quit OpenMV IDE, replug the Nicla.

## 2. Laptop
```bash
cd laptop
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GOOGLE_PLACES_API_KEY=your_key   # Places API (New) + Geocoding API enabled
python app.py                           # auto-detects the Nicla
# python app.py --port /dev/cu.usbmodemXXXX   if auto-detect fails
# python app.py --simulate                    no camera: use the demo controls
```
- Operator dashboard: **http://127.0.0.1:5050** (camera status, confidence, demo controls).
  Keep it open in a browser on the laptop and allow location access; it provides the location.
- Audience dashboard: **http://127.0.0.1:5051**, or `http://<laptop-ip>:5051` from another
  device on the same Wi-Fi. Read-only; control endpoints are not exposed there.

`python test_wallet.py` runs the stack-logic tests.

## Known limits
- The camera only sees the top note: several notes inserted at once count as one
- State is in memory; restarting the app empties the wallet.


