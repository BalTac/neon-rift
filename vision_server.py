"""Florence-2 micro vision server for Neon Rift.

Exposes a tiny HTTP API on 127.0.0.1:VISION_PORT that loads a light vision
model (default: microsoft/Florence-2-base-ft) and analyzes screenshots from
the game. The Neon Rift LLM coach then uses this caption + region info as
extra context to pick a tactical stance.

Endpoints:
  GET  /health     -> {loaded, loading, device, model, error}
  POST /analyze    -> {caption, regions, size, timing_ms}
                      body: {"image": "data:image/jpeg;base64,...",
                             "regions": true|false}

Env vars:
  VISION_PORT   default 8077
  VISION_MODEL  default microsoft/Florence-2-base-ft
  VISION_DEVICE auto|cpu|cuda  (default auto)
"""

import base64
import io
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


PORT = int(os.environ.get("VISION_PORT", "8077"))
MODEL_ID = os.environ.get("VISION_MODEL", "microsoft/Florence-2-base-ft")
DEVICE_PREF = os.environ.get("VISION_DEVICE", "auto").lower()


_state = {
    "model": None,
    "processor": None,
    "device": None,
    "dtype": None,
    "error": None,
    "loading": False,
    "loaded": False,
    "load_started": None,
    "load_finished": None,
}
_infer_lock = threading.Lock()


def log(*args):
    print("[vision]", *args, flush=True)


def load_model():
    if _state["loaded"] or _state["loading"]:
        return
    _state["loading"] = True
    _state["load_started"] = time.time()
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoProcessor

        dev = DEVICE_PREF
        if dev == "auto":
            dev = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.float16 if dev == "cuda" else torch.float32

        log(f"loading {MODEL_ID} on {dev} ({dtype})...")
        processor = AutoProcessor.from_pretrained(MODEL_ID, trust_remote_code=True)
        model = AutoModelForCausalLM.from_pretrained(
            MODEL_ID, trust_remote_code=True, torch_dtype=dtype
        ).to(dev).eval()

        _state["model"] = model
        _state["processor"] = processor
        _state["device"] = dev
        _state["dtype"] = str(dtype).split(".")[-1]
        _state["loaded"] = True
        _state["load_finished"] = time.time()
        log(f"ready in {_state['load_finished'] - _state['load_started']:.1f}s.")
    except Exception as exc:
        _state["error"] = f"{type(exc).__name__}: {exc}"
        log("load error:", _state["error"])
        traceback.print_exc()
    finally:
        _state["loading"] = False


def run_task(image, task):
    import torch

    model = _state["model"]
    processor = _state["processor"]
    dev = _state["device"]

    inputs = processor(text=task, images=image, return_tensors="pt")
    moved = {}
    for k, v in inputs.items():
        if hasattr(v, "to"):
            v = v.to(dev)
            if k == "pixel_values" and dev == "cuda":
                v = v.half()
        moved[k] = v

    with torch.no_grad():
        generated = model.generate(
            input_ids=moved["input_ids"],
            pixel_values=moved["pixel_values"],
            max_new_tokens=256,
            num_beams=1,
            do_sample=False,
            early_stopping=False,
        )
    raw = processor.batch_decode(generated, skip_special_tokens=False)[0]
    parsed = processor.post_process_generation(
        raw, task=task, image_size=(image.width, image.height)
    )
    return parsed.get(task, parsed)


def analyze(image_bytes, want_regions=True):
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    if img.width > 512 or img.height > 512:
        img.thumbnail((512, 512))

    with _infer_lock:
        t0 = time.time()
        caption = run_task(img, "<MORE_DETAILED_CAPTION>")
        t1 = time.time()
        regions = None
        if want_regions:
            try:
                regions = run_task(img, "<DENSE_REGION_CAPTION>")
            except Exception as exc:
                regions = {"error": f"{type(exc).__name__}: {exc}"}
        t2 = time.time()

    return {
        "caption": caption,
        "regions": regions,
        "size": [img.width, img.height],
        "timing_ms": {
            "caption": int((t1 - t0) * 1000),
            "regions": int((t2 - t1) * 1000),
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log("http", self.address_string(), fmt % args)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")

    def _json(self, status, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {
                "loaded": _state["loaded"],
                "loading": _state["loading"],
                "device": _state["device"],
                "dtype": _state["dtype"],
                "model": MODEL_ID,
                "error": _state["error"],
            })
            return
        self.send_response(404)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path != "/analyze":
            self.send_response(404)
            self._cors()
            self.end_headers()
            return

        if not _state["loaded"]:
            self._json(503, {
                "error": "model not loaded",
                "loading": _state["loading"],
                "detail": _state["error"],
            })
            return

        try:
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length) if length else b""
            req = json.loads(body or b"{}")
            data_url = req.get("image", "")
            if not data_url:
                self._json(400, {"error": "missing image"})
                return
            if "," in data_url and data_url.startswith("data:"):
                data_url = data_url.split(",", 1)[1]
            image_bytes = base64.b64decode(data_url)
            want_regions = bool(req.get("regions", True))
            result = analyze(image_bytes, want_regions=want_regions)
            self._json(200, result)
        except Exception as exc:
            traceback.print_exc()
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})


def main():
    threading.Thread(target=load_model, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log(f"listening on http://127.0.0.1:{PORT}  (model={MODEL_ID})")
    log(f"health: http://127.0.0.1:{PORT}/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()
