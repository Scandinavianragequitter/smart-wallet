# Smart wallet - Nicla Vision camera script (OpenMV)
#
# Classifies what is on top of the wallet stack and streams one JSON line per
# frame over USB, e.g.  {"label": "20", "conf": 0.93}
# The laptop app reads these lines.
#
# Setup: copy these two files from your Edge Impulse export onto the Nicla's
# drive (the "NO NAME" / "OPENMV" disk that appears when it's plugged in):
#     ei-model.tflite  -> rename to  trained.tflite
#     ei-model.txt     -> rename to  labels.txt
# Then in OpenMV IDE: Tools > Save open script to OpenMV Cam (as main.py),
# and CLOSE OpenMV IDE before starting the laptop app (the IDE holds the port).

import sensor, time, ml, uos, gc, json

sensor.reset()
sensor.set_pixformat(sensor.RGB565)
sensor.set_framesize(sensor.QVGA)       # 320x240
sensor.set_windowing((240, 240))        # same square crop as the Edge Impulse example
sensor.skip_frames(time=2000)


def load_model():
    """Model files on the Nicla's drive, or a model built into the firmware."""
    for model_file, labels_file in (("trained.tflite", "labels.txt"),
                                    ("ei-model.tflite", "ei-model.txt")):
        try:
            size = uos.stat(model_file)[6]
        except OSError:
            continue
        model = ml.Model(model_file, load_to_fb=size > (gc.mem_free() - 64 * 1024))
        labels = [l.strip() for l in open(labels_file).read().split("\n") if l.strip()]
        return model, labels
    model = ml.Model("trained")          # Edge Impulse firmware with built-in model
    return model, list(model.labels)


model, labels = load_model()
FPS_LIMIT = 10
min_dt = 1000 // FPS_LIMIT
last = time.ticks_ms()

while True:
    img = sensor.snapshot()
    scores = model.predict([img])[0].flatten().tolist()
    best = 0
    for i in range(1, len(scores)):
        if scores[i] > scores[best]:
            best = i
    now = time.ticks_ms()
    if time.ticks_diff(now, last) >= min_dt:
        last = now
        print(json.dumps({"label": labels[best], "conf": round(scores[best], 3)}))
