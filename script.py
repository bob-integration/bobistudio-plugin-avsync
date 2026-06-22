# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

# ─────────────────────────────────────────────────────────────────────────────
# Mire synchro A/V — générateur de test (catégorie Sources).
# Produit un shm vidéo (YUV420p) + un shm audio (s24be 8ch) au format MXL.
#   - FLASH plein écran + BIP sinus SYNCHRONES (les deux calés sur la même horloge
#     time.monotonic() → pas de signalisation inter-thread, alignés au battement).
#   - Compteur sec.frame, barre de balayage (détection de saccades).
#   - Overlay personnalisable : logo entreprise + nom du système + localisation
#     (repris des réglages Personnalisation, injectés au déploiement), format vidéo,
#     et texte libre.
#
# Ce fichier est un TEMPLATE str.format() : SEULS {config} / {hostname} /
# {plugin_version} sont des placeholders. TOUTE autre accolade littérale doit être
# DOUBLÉE {{ }} sinon str.format échoue.
# ─────────────────────────────────────────────────────────────────────────────
import os, io, mmap, struct, time, json, base64, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import numpy as np
import bobimxl   # migration MXL Phase 1 : sortie VIDÉO via Writer (audio = legacy mmap)
from PIL import Image, ImageDraw, ImageFont
try:
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE = Image.LANCZOS

CONFIG         = {config}
HOSTNAME       = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

WIDTH  = int(CONFIG.get("width") or 1280);  WIDTH  -= WIDTH % 2
HEIGHT = int(CONFIG.get("height") or 720);  HEIGHT -= HEIGHT % 2
def _rate_nd(v):
    """Cadence → (num, den) EXACT (fractionnaire NTSC = N*1000/1001 ; accepte \"30000/1001\")."""
    try:
        if isinstance(v, str) and "/" in v:
            a, b = v.split("/"); return int(a), int(b)
        f = float(v or 25)
    except Exception:
        return 25, 1
    n = round(f)
    if abs(f - n) < 0.01: return (n or 25), 1
    nominal = round(f * 1001.0 / 1000.0); return nominal * 1000, 1001
FPS_NUM, FPS_DEN = _rate_nd(CONFIG.get("fps") or 25)
FPS    = max(1, round(FPS_NUM / FPS_DEN))
BEAT_SEC = float(CONFIG.get("beat_sec") or 1.0) or 1.0
FLASH_MS = int(CONFIG.get("flash_ms") or 60)
TONE_HZ  = float(CONFIG.get("tone_hz") or 1000.0)
def _as_bool(v, default=False):
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)
SHOW_BRAND  = _as_bool(CONFIG.get("show_brand"), default=True)
CUSTOM_TEXT = str(CONFIG.get("custom_text") or "")

BRAND_NAME = str(CONFIG.get("brand_system_name") or "")
BRAND_LOC  = str(CONFIG.get("brand_location") or "")
BRAND_ORG  = str(CONFIG.get("brand_org_name") or "")
LOGO_B64   = CONFIG.get("brand_logo_b64") or ""

FORMAT_STR = "%dx%d @%dp" % (WIDTH, HEIGHT, FPS)

# ─── formats shm (identiques au pipeline MXL) ────────────────────────────────
CHROMA = str(CONFIG.get("chroma") or "422")
# Profondeur shm 8/10/12 bits. La quantification est paramétrée (_SCALE/_MAXV/_NP_DT) :
# en 8 bits _SCALE=1 et dtype uint8 → sortie byte-identique, pas de surcoût uint16.
BIT_DEPTH = int(CONFIG.get("bit_depth") or 8)
_DEEP  = BIT_DEPTH >= 10
_BPS   = 2 if _DEEP else 1
_NP_DT = np.uint16 if _DEEP else np.uint8
_SCALE = 1 << (BIT_DEPTH - 8)          # 1 / 4 / 16 — facteur d'échelle studio 8→N bits
_MAXV  = (1 << BIT_DEPTH) - 1           # 255 / 1023 / 4095
_NEUTRAL = 1 << (BIT_DEPTH - 1)         # 128 / 512 / 2048
_CW = {{"420": 2, "422": 2, "444": 1}}.get(CHROMA, 2)
_CH = {{"420": 2, "422": 1, "444": 1}}.get(CHROMA, 1)
PIX_FMT = ({{"420": "yuv420p", "422": "yuv422p", "444": "yuv444p"}}.get(CHROMA, "yuv422p")
           + (("12le" if BIT_DEPTH >= 12 else "10le") if _DEEP else ""))
UV_W = WIDTH // _CW
UV_H = HEIGHT // _CH
V_HEADER_SIZE = 64
V_RING_SIZE   = CONFIG.get("shm_video_ring", 10)
Y_SIZE        = WIDTH * HEIGHT * _BPS
UV_SIZE       = UV_W * UV_H * _BPS
V_FRAME_SIZE  = Y_SIZE + 2 * UV_SIZE
V_TOTAL_SIZE  = V_HEADER_SIZE + V_RING_SIZE * V_FRAME_SIZE

A_SAMPLE_RATE = 48000
A_CHANNELS    = 8
A_HEADER_SIZE = 64
A_RING_SIZE   = CONFIG.get("shm_audio_ring", 100)
A_CHUNK_SIZE  = (A_SAMPLE_RATE // 1000) * A_CHANNELS * 3   # 1152 (1 ms s24be 8ch)
A_TOTAL_SIZE  = A_HEADER_SIZE + A_RING_SIZE * A_CHUNK_SIZE

VIDEO_SHM = HOSTNAME
AUDIO_SHM = HOSTNAME + "_audio"
inst = bobimxl.Instance()   # domaine MXL ($MXL_DOMAIN ou /dev/shm/mxl) — sortie vidéo

metrics_lock = threading.Lock()
metrics = {{"fps": 0.0, "frame_index": 0, "playing": True, "plugin_version": PLUGIN_VERSION}}

# Genlock broadcast : origine d'horloge PARTAGÉE vidéo+audio sur l'horloge REALTIME (CLOCK_REALTIME,
# disciplinée PTP par phc2sys) → flash/bip alignés ET traçables PTP ; la sortie est cadencée sur la
# grille epoch (write_ts = instant de grille). off → cadence libre (monotonic héritée).
_gl = CONFIG.get("genlock", True)
GENLOCK = _gl if isinstance(_gl, bool) else str(_gl).strip().lower() in ("1", "true", "yes", "on")
def _grid_next(now_s, interval_s):
    import math as _m
    return (_m.floor(now_s / interval_s) + 1) * interval_s
def _grid_ns(k):
    return round(k * 1_000_000_000 * FPS_DEN / FPS_NUM)
def _grid_k(ns):
    return ns * FPS_NUM // (1_000_000_000 * FPS_DEN)
def _grid_sleep_until(deadline_ns):
    dt = (deadline_ns - time.clock_gettime_ns(time.CLOCK_REALTIME)) / 1e9
    if dt > 0: time.sleep(dt)
T0 = time.time() if GENLOCK else time.monotonic()   # origine partagée vidéo + audio


# ─── shm helpers ─────────────────────────────────────────────────────────────
def _open_out(path, size):
    with open(path, "wb") as f:
        f.write(b"\x00" * size)
    f = open(path, "r+b")
    return f, mmap.mmap(f.fileno(), size)

def _write_video(shm, frame_bytes, idx, ts=None):
    slot = idx % V_RING_SIZE
    off = V_HEADER_SIZE + slot * V_FRAME_SIZE
    shm[off:off + V_FRAME_SIZE] = frame_bytes
    shm[0:16] = struct.pack("QQ", idx, ts if ts is not None else time.time_ns())

def _write_audio(shm, chunk_bytes, idx):
    slot = idx % A_RING_SIZE
    off = A_HEADER_SIZE + slot * A_CHUNK_SIZE
    shm[off:off + A_CHUNK_SIZE] = chunk_bytes
    shm[0:24] = struct.pack("QQIHH", idx, time.time_ns(), A_SAMPLE_RATE, A_CHANNELS, 24)

def _rgb_to_yuv420(rgb):
    """rgb uint8 (H,W,3) → bytes Y + U + V (4:2:0), BT.601 studio."""
    r = rgb[:, :, 0].astype(np.float32)
    g = rgb[:, :, 1].astype(np.float32)
    b = rgb[:, :, 2].astype(np.float32)
    y = ( 0.257 * r + 0.504 * g + 0.098 * b + 16.0)
    u = (-0.148 * r - 0.291 * g + 0.439 * b + 128.0)
    v = ( 0.439 * r - 0.368 * g - 0.071 * b + 128.0)
    # Mise à l'échelle studio 8→N bits (×_SCALE) ; en 8 bits _SCALE=1, dtype uint8 (inchangé).
    y_u8 = np.clip(y * _SCALE, 0, _MAXV).astype(_NP_DT)
    u_u8 = np.clip(u[::_CH, ::_CW] * _SCALE, 0, _MAXV).astype(_NP_DT)
    v_u8 = np.clip(v[::_CH, ::_CW] * _SCALE, 0, _MAXV).astype(_NP_DT)
    return y_u8.tobytes() + u_u8.tobytes() + v_u8.tobytes()


# ─── polices ─────────────────────────────────────────────────────────────────
def _font(px):
    px = max(8, int(px))
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, px)
        except Exception:
            pass
    return ImageFont.load_default()

FONT_BIG = _font(HEIGHT // 5)
FONT_MED = _font(max(14, HEIGHT // 26))
_probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
def _tsize(txt, font):
    try:
        bb = _probe.textbbox((0, 0), txt, font=font)
        return bb[2] - bb[0], bb[3] - bb[1]
    except Exception:
        return (len(txt) * 8, 12)


# ─── logo (base64 injecté au déploiement) ────────────────────────────────────
def _load_logo():
    if not (SHOW_BRAND and LOGO_B64):
        return None
    try:
        img = Image.open(io.BytesIO(base64.b64decode(LOGO_B64))).convert("RGBA")
        target_h = max(24, HEIGHT // 9)
        if img.height != target_h:
            w = max(1, int(img.width * target_h / img.height))
            img = img.resize((w, target_h), RESAMPLE)
        return img
    except Exception as e:
        print("logo illisible: %s" % e)
        return None

LOGO = _load_logo()


# ─── overlay (logo + textes marque/format/perso), composé à la demande ──────
def _build_imgs(show_brand, custom_text):
    """Construit (img_black, img_white) avec l'overlay courant. Appelé au boot et
    à chaud quand show_brand ou custom_text change."""
    ov = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    tx = 16
    if LOGO is not None:
        ov.alpha_composite(LOGO, (16, 16))
        tx = 16 + LOGO.width + 14
    yy = 18
    if show_brand:
        for ln in (BRAND_NAME, BRAND_LOC):
            if ln:
                d.text((tx, yy), ln, font=FONT_MED, fill=(255, 255, 255, 235))
                yy += _tsize(ln, FONT_MED)[1] + 6
    fh = _tsize(FORMAT_STR, FONT_MED)[1]
    d.text((16, HEIGHT - fh - 16), FORMAT_STR, font=FONT_MED, fill=(170, 215, 255, 235))
    if custom_text:
        cw, ch = _tsize(custom_text, FONT_MED)
        d.text(((WIDTH - cw) // 2, HEIGHT - ch - 16), custom_text, font=FONT_MED, fill=(255, 240, 170, 235))
    blk = Image.alpha_composite(Image.new("RGBA", (WIDTH, HEIGHT), (8, 8, 8, 255)), ov).convert("RGB")
    wht = Image.alpha_composite(Image.new("RGBA", (WIDTH, HEIGHT), (236, 236, 236, 255)), ov).convert("RGB")
    return blk, wht

IMG_BLACK, IMG_WHITE = _build_imgs(SHOW_BRAND, CUSTOM_TEXT)


def _frame_bytes(flash, sec, fis, bar_x, img_black=None, img_white=None):
    if img_black is None: img_black = IMG_BLACK
    if img_white is None: img_white = IMG_WHITE
    im = (img_white if flash else img_black).copy()
    d = ImageDraw.Draw(im)
    # barre de balayage verticale
    bw = max(2, WIDTH // 200)
    d.rectangle([bar_x, 0, bar_x + bw, HEIGHT - 1], fill=(80, 160, 240))
    # compteur centré : secondes.frame
    txt = "%d.%02d" % (sec, fis)
    tw, th = _tsize(txt, FONT_BIG)
    col = (10, 10, 10) if flash else (255, 255, 255)
    d.text(((WIDTH - tw) // 2, (HEIGHT - th) // 2), txt, font=FONT_BIG, fill=col)
    return _rgb_to_yuv420(np.asarray(im))


# ─── bip audio : blocs de 1 ms pré-calculés (sinus float32 8ch) — flux MXL ──
# Migration MXL Phase 3 : on produit des blocs float32 (n,8) normalisés [-1,1] au lieu de s24be ;
# l'AudioWriter les écrit dans le flux MXL audio (bobimxl).
A_SPMS = A_SAMPLE_RATE // 1000   # 48 samples = 1 ms
def _beep_chunks(flash_ms=None, tone_hz=None):
    n_ms = max(1, int(flash_ms if flash_ms is not None else FLASH_MS))
    hz   = float(tone_hz if tone_hz is not None else TONE_HZ)
    nsamp = A_SPMS * n_ms
    t = np.arange(nsamp, dtype=np.float64) / A_SAMPLE_RATE
    w = (np.sin(2 * np.pi * hz * t) * 0.5).astype(np.float32)        # mono, niveau −6 dBFS
    inter = np.tile(w[:, None], (1, A_CHANNELS))                      # (nsamp, 8) float32
    return [inter[i * A_SPMS:(i + 1) * A_SPMS].copy() for i in range(n_ms)]   # blocs (48,8)

BEEP = _beep_chunks()
SILENCE = np.zeros((A_SPMS, A_CHANNELS), dtype=np.float32)
BEAT_MS = max(1, int(round(BEAT_SEC * 1000)))

# ─── état live : params soft applicables à chaud via POST :8082/config ─────
_live_lock = threading.Lock()
_live = {{
    "beat_sec": BEAT_SEC, "flash_ms": int(FLASH_MS), "tone_hz": float(TONE_HZ),
    "show_brand": SHOW_BRAND, "custom_text": CUSTOM_TEXT,
    "beep": BEEP, "beat_ms": BEAT_MS, "img_black": IMG_BLACK, "img_white": IMG_WHITE,
}}

def _apply_live(patch):
    """Met à jour les params soft à chaud et recompute les données dérivées."""
    with _live_lock:
        _live.update(patch)
        if "beat_sec" in patch:
            _live["beat_ms"] = max(1, int(round(_live["beat_sec"] * 1000)))
        if "flash_ms" in patch or "tone_hz" in patch:
            _live["beep"] = _beep_chunks(_live["flash_ms"], _live["tone_hz"])
        if "show_brand" in patch or "custom_text" in patch:
            blk, wht = _build_imgs(_live["show_brand"], _live["custom_text"])
            _live["img_black"] = blk; _live["img_white"] = wht


# ─── threads de génération ───────────────────────────────────────────────────
def video_feeder():
    w = bobimxl.Writer(inst, VIDEO_SHM, WIDTH, HEIGHT, CHROMA, BIT_DEPTH, FPS_NUM, FPS_DEN,
                       index_mode=("tai" if GENLOCK else "free"))
    idx = 1
    period = FPS_DEN / FPS_NUM
    _clk = time.time if GENLOCK else time.monotonic
    next_t = _grid_next(_clk(), period) if GENLOCK else _clk()
    frame_k = _grid_k(time.clock_gettime_ns(time.CLOCK_REALTIME)) + 2 if GENLOCK else 0
    win_cnt = 0; win_start = time.time()
    while True:
        if GENLOCK:
            _grid_sleep_until(_grid_ns(frame_k))
            now = _clk()
        else:
            now = _clk()
            if now < next_t:
                time.sleep(next_t - now); now = _clk()
        with _live_lock:
            beat_sec = _live["beat_sec"]; flash_ms = _live["flash_ms"]
            img_b = _live["img_black"];   img_w = _live["img_white"]
        el = (next_t if GENLOCK else now) - T0     # instant de présentation (grille en genlock)
        phase = el % beat_sec
        flash = phase < (flash_ms / 1000.0)
        sec = int(el)
        fis = int((el - sec) * FPS)
        bar_x = int((phase / beat_sec) * (WIDTH - 1))
        _fb = _frame_bytes(flash, sec, fis, bar_x, img_b, img_w)
        _i, _gi, _vw = w.open_grain(index=(frame_k if GENLOCK else None))
        _vw[:len(_fb)] = np.frombuffer(_fb, dtype=np.uint8)
        w.commit(_gi); idx += 1
        win_cnt += 1
        _e = time.time() - win_start
        if _e >= 1.0:
            with metrics_lock:
                metrics["fps"] = round(win_cnt / _e, 1); metrics["frame_index"] = idx
            win_cnt = 0; win_start = time.time()
        next_t += period
        if next_t < now:
            next_t = (_grid_next(now, period) if GENLOCK else now + period)
        if GENLOCK:
            frame_k += 1
            now_k = _grid_k(time.clock_gettime_ns(time.CLOCK_REALTIME))
            if frame_k < now_k: frame_k = now_k + 1   # rattrapage si retard système

def audio_feeder():
    aw = bobimxl.AudioWriter(inst, AUDIO_SHM, channels=A_CHANNELS, sample_rate=A_SAMPLE_RATE,
                             index_mode=("tai" if GENLOCK else "free"))
    written_ms = 0
    _aclk = time.time if GENLOCK else time.monotonic
    while True:
        due = int((_aclk() - T0) * 1000)
        with _live_lock:
            beat_ms = _live["beat_ms"]; beep = _live["beep"]
        guard = 0
        while written_ms < due and guard < 2000:
            phase_ms = written_ms % beat_ms
            chunk = beep[phase_ms] if phase_ms < len(beep) else SILENCE
            try: aw.write(chunk)
            except Exception: pass
            written_ms += 1; guard += 1
        time.sleep(0.003)


# ─── HTTP : :8080 metrics + :8082 control (/state) ───────────────────────────
class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with metrics_lock: payload = dict(metrics)
        body = json.dumps(payload).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

class ControlHandler(BaseHTTPRequestHandler):
    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/state":
            with metrics_lock: m = dict(metrics)
            with _live_lock: lv = dict(_live)
            s = {{"video_shm": VIDEO_SHM, "audio_shm": AUDIO_SHM,
                  "width": WIDTH, "height": HEIGHT, "fps": FPS, "chroma": CHROMA,
                  "format": FORMAT_STR,
                  "beat_sec": lv["beat_sec"], "flash_ms": lv["flash_ms"],
                  "tone_hz": lv["tone_hz"], "show_brand": lv["show_brand"],
                  "custom_text": lv["custom_text"],
                  "brand_system_name": BRAND_NAME, "brand_location": BRAND_LOC,
                  "has_logo": LOGO is not None,
                  "fps_live": m.get("fps"), "frame_index": m.get("frame_index"),
                  "plugin_version": PLUGIN_VERSION}}
            self._send_json(200, s)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path != "/config":
            self.send_response(404); self.end_headers(); return
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n)) if n else {{}}
        except Exception:
            self._send_json(400, {{"error": "JSON invalide"}}); return
        _validators = {{
            "beat_sec": lambda v: float(v),
            "flash_ms": lambda v: int(v),
            "tone_hz":  lambda v: float(v),
            "show_brand": lambda v: bool(v),
            "custom_text": lambda v: str(v),
        }}
        patch = {{}}
        for k, cast in _validators.items():
            if k in body:
                try: patch[k] = cast(body[k])
                except Exception: pass
        if patch:
            _apply_live(patch)
        self._send_json(200, {{"ok": True, "applied": list(patch.keys())}})

    def log_message(self, *a): pass


threading.Thread(target=lambda: HTTPServer(("0.0.0.0", 8080), MetricsHandler).serve_forever(), daemon=True).start()
threading.Thread(target=lambda: HTTPServer(("0.0.0.0", 8082), ControlHandler).serve_forever(), daemon=True).start()
threading.Thread(target=video_feeder, daemon=True).start()
threading.Thread(target=audio_feeder, daemon=True).start()

while True:
    time.sleep(1)
