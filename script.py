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
import os, io, time, json, base64, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import numpy as np
import bobimxl   # migration MXL : sortie vidéo (Writer) + audio bip (AudioWriter) — Phases 1/3


def _mxl_lib_state():
    """Variante libmxl réellement chargée (baseline / x86-64-v3) — diagnostic seul, ne doit
    JAMAIS faire échouer /state."""
    try:
        return bobimxl.lib_info()
    except Exception:
        return None
from PIL import Image, ImageDraw, ImageFont
try:
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE = Image.LANCZOS

CONFIG         = {config}
HOSTNAME       = "{hostname}"
PLUGIN_VERSION = "{plugin_version}"

# ─── Niveau de log ─────────────────────────────────────────────────────────
# `log_level` (config_schema du plugin, défaut « info ») filtre les impressions du script.
# Le critère n'est PAS « verbeux vs silencieux » mais ÉVÉNEMENT vs MÉTRIQUE :
#   debug   — le lance-flammes : par trame, par bande, décisions internes
#   info    — ÉVÉNEMENTS rares et signifiants  ← DÉFAUT (toujours visible) : démarrage/
#             arrêt, session ouverte/fermée, changement de format, reconnexion, repli sur
#             un chemin dégradé, entrée qui apparaît/disparaît, rebascule.
#   warning — anomalies et replis subis
#   error   — échecs
# RÈGLE 1 : après une panne, le journal PAR DÉFAUT doit permettre de RECONSTITUER
#   l'histoire. Élever le niveau après coup ne récupère RIEN : ce qui n'a pas été écrit
#   est perdu. On ne coupe donc pas l'information, on coupe la redondance.
# RÈGLE 2 : une MÉTRIQUE PÉRIODIQUE (fps, compteurs) ne se journalise PAS — elle est déjà
#   publiée sur :8080 et échantillonnée par l'orchestrateur. La journaliser duplique la
#   mesure ET consomme la fenêtre de rétention (journal Docker non roté : le bruit purge
#   les lignes utiles anciennes). Au mieux `debug`.
# RÈGLE 3 : un événement qui peut partir EN RAFALE s'AGRÈGE sur une fenêtre et sort en UNE
#   ligne périodique (« N frames lentes sur la dernière minute, pire … ») — le signal
#   reste, le spam disparaît.
# Réglable à chaud, sans redéployer, quand le plugin expose l'endpoint de contrôle :
# POST :8082/log_level {{"level": "debug"}} (exposé aux macros via param_tree/actions).
_LOG_ORDER = {{"debug": 10, "info": 20, "warning": 30, "error": 40}}
LOG_LEVEL = str(CONFIG.get("log_level") or "info").strip().lower()
if LOG_LEVEL not in _LOG_ORDER:
    LOG_LEVEL = "info"
_LOG_MIN = _LOG_ORDER[LOG_LEVEL]


def log(msg, niveau="info"):
    """Impression gatée par le niveau de log courant (défaut du message : « info »)."""
    if _LOG_ORDER.get(niveau, 20) >= _LOG_MIN:
        print(msg, flush=True)


def set_log_level(niveau):
    """Change le niveau à chaud. Renvoie True si le niveau est reconnu."""
    global LOG_LEVEL, _LOG_MIN
    lv = str(niveau or "").strip().lower()
    if lv not in _LOG_ORDER:
        return False
    LOG_LEVEL, _LOG_MIN = lv, _LOG_ORDER[lv]
    return True



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
# ── Générateur de TIMECODE (option) : heure du jour (même horloge de présentation que la
# trame — grille PTP en genlock) incrustée sous le compteur ET émise en flux ANC ATC RP 188
# (RFC 8331, motif player) sur `<hostname>_anc`, à l'index de grain de la vidéo → un multiview
# câblé dessus affiche le TC exactement aligné sur l'image. Désactivé par défaut (coût nul).
TC_ENABLE = _as_bool(CONFIG.get("tc_enable"), default=False)
# Fuseau horaire injecté par le hook before_deploy (les images runtime sont en UTC) : le
# timecode affiche l'HEURE LOCALE de la régie. Sans tz → heure du container (UTC).
TZ_NAME = str(CONFIG.get("tz") or "").strip()
if TZ_NAME:
    os.environ["TZ"] = TZ_NAME
    try:
        time.tzset()
    except Exception:
        pass
# Horloge du nœud = échelle PTP (TAI, cf. docs/reference/PTP_CLOCK.md) : l'heure CIVILE affichée = horloge
# − tai_utc_offset_s (mesuré et injecté par before_deploy ; 0 si nœud en heure civile).
try:
    TAI_UTC_OFFSET_S = max(0, int(CONFIG.get("tai_utc_offset_s") or 0))
except (TypeError, ValueError):
    TAI_UTC_OFFSET_S = 0

# ── MODE TRANCHE (chantier latence sous-trame, cf. patch mxl-planar-slices) ─────────────────
# slice_mode=true → la trame RENDUE (déjà complète : le rendu mire est instantané) est émise
# PAR BANDES avec commit progressif (validSlices=1..N, pattern delay _emit_frame_sliced) →
# l'aval slice-aware démarre sur la 1ʳᵉ bande. Gain propre faible ici — c'est de la COHÉRENCE
# de chaîne (une mire tranchée exerce le chemin sous-trame de bout en bout). L'audio, le
# timing flash/bip et le media_ts sont STRICTEMENT inchangés (instrument de mesure A/V).
# slice_mode absent/False → comportement octet-identique à l'historique.
SLICE_MODE  = _as_bool(CONFIG.get("slice_mode"), default=False)
SLICE_LINES = max(1, int(CONFIG.get("slice_lines") or 36))
_SLICE_TARGET = max(1, 1080 // SLICE_LINES)

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

def _out_slice_h(oh, ch=1):
    """MODE TRANCHE — slice_height de la sortie (même algo que delay/pyramide) : le plus petit
    diviseur sh de oh avec sh ≥ max(1, oh // _SLICE_TARGET), MULTIPLE de ch (lignes chroma
    entières par bande). Aucun diviseur raisonnable (sh > oh//2) → 0 = inéligible (whole-frame,
    chemin historique)."""
    lo = max(1, oh // _SLICE_TARGET)
    for sh in range(lo, oh // 2 + 1):
        if oh % sh == 0 and sh % ch == 0:
            return sh
    return 0

# Éligibilité tranche : la mire est toujours PROGRESSIVE → seule la divisibilité compte.
V_SLICE_H = _out_slice_h(HEIGHT, _CH) if SLICE_MODE else 0
_V_TOTAL_SLICES = (HEIGHT // V_SLICE_H) if V_SLICE_H else 1

def _commit_sliced(w, gi):
    """MODE TRANCHE — le rendu 0.10.0 écrit DIRECTEMENT dans le grain (zéro-copie) : plus de
    copie par bandes, il ne reste que le commit progressif (validSlices=1..N, quasi gratuit —
    pattern delay _emit_frame_sliced) qui réveille l'aval slice-aware bande par bande. Commit
    FINAL garanti (un grain laissé partiel ne serait jamais lisible)."""
    try:
        for k in range(1, _V_TOTAL_SLICES):
            w.commit(gi, valid_slices=k)   # réveille l'aval à chaque bande
    finally:
        w.commit(gi, valid_slices=None)

A_SAMPLE_RATE = 48000
A_CHANNELS    = 8
A_HEADER_SIZE = 64
A_RING_SIZE   = CONFIG.get("shm_audio_ring", 100)
A_CHUNK_SIZE  = (A_SAMPLE_RATE // 1000) * A_CHANNELS * 3   # 1152 (1 ms s24be 8ch)
A_TOTAL_SIZE  = A_HEADER_SIZE + A_RING_SIZE * A_CHUNK_SIZE

VIDEO_SHM = HOSTNAME
AUDIO_SHM = HOSTNAME + "_audio"
ANC_SHM   = HOSTNAME + "_anc"    # flux ANC du générateur de timecode (option tc_enable)
inst = bobimxl.Instance()   # domaine MXL ($MXL_DOMAIN ou /dev/shm/mxl) — sortie vidéo

metrics_lock = threading.Lock()
metrics = {{"fps": 0.0, "frame_index": 0, "playing": True, "own_latency_ms": 0.0,
            "slice_mode": bool(V_SLICE_H), "plugin_version": PLUGIN_VERSION}}

# Genlock broadcast : origine d'horloge PARTAGÉE vidéo+audio sur l'horloge REALTIME (CLOCK_REALTIME,
# disciplinée PTP par phc2sys) → flash/bip alignés ET traçables PTP ; la sortie est cadencée sur la
# grille epoch (write_ts = instant de grille). off → cadence libre (monotonic héritée).
_gl = CONFIG.get("genlock", True)
GENLOCK = _gl if isinstance(_gl, bool) else str(_gl).strip().lower() in ("1", "true", "yes", "on")
def _grid_next(now_s, interval_s):
    import math as _m
    return (_m.floor(now_s / interval_s) + 1) * interval_s
T0 = time.time() if GENLOCK else time.monotonic()   # origine partagée vidéo + audio


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


def _yuv_const(r, g, b):
    """YUV studio d'une couleur RGB unie — MÊMES coefficients que _rgb_to_yuv420 (le patch
    rapide 0.10.0 doit rester colorimétriquement identique au chemin historique)."""
    y = 0.257 * r + 0.504 * g + 0.098 * b + 16.0
    u = -0.148 * r - 0.291 * g + 0.439 * b + 128.0
    v = 0.439 * r - 0.368 * g - 0.071 * b + 128.0
    def _q(x):
        return int(min(_MAXV, max(0, round(x * _SCALE))))
    return _q(y), _q(u), _q(v)

# Couleurs patchées par-frame (perf 0.10.0) : barre de balayage + compteur sur fond noir/flash.
BAR_YUV          = _yuv_const(80, 160, 240)
TXT_YUV_ON_BLACK = _yuv_const(255, 255, 255)
TXT_YUV_ON_FLASH = _yuv_const(10, 10, 10)


# ─── polices ─────────────────────────────────────────────────────────────────
# Catalogue des polices EMBARQUÉES dans l'image runtime (bloc dupliqué, ASSUMÉ, du multiview —
# `plugins/<type>/script.py` est autonome et ne peut pas importer de code de l'orchestrateur ;
# même image compute (cf. _compute_runtime/Dockerfile fonts-dejavu-core/liberation/inter/roboto/
# firacode) donc mêmes chemins). Chaque clé → liste de chemins candidats.
_FONT_FILES = {{
    "dejavu-sans":          ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "dejavu-sans-bold":     ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "dejavu-serif":         ["/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"],
    "dejavu-mono":          ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"],
    "liberation-sans":      ["/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"],
    "liberation-sans-bold": ["/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
    "liberation-mono":      ["/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"],
    "inter":    ["/usr/share/fonts/truetype/inter/Inter-Regular.otf",
                 "/usr/share/fonts/opentype/inter/Inter-Regular.otf",
                 "/usr/share/fonts/truetype/inter/InterVariable.ttf"],
    "roboto":   ["/usr/share/fonts/truetype/roboto/unhinted/Roboto-Regular.ttf",
                 "/usr/share/fonts/truetype/roboto/hinted/Roboto-Regular.ttf",
                 "/usr/share/fonts/truetype/roboto/Roboto-Regular.ttf"],
    "firacode": ["/usr/share/fonts/truetype/firacode/FiraCode-Regular.ttf",
                 "/usr/share/fonts/opentype/firacode/FiraCode-Regular.otf"],
}}
_DEFAULT_FONT_FILE = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# ─── Bibliothèque de polices poussée par l'orchestrateur (motif éprouvé du multiview) ────────
# CONFIG["font_library"] = [{{"key": "lib:<sha16>", "name", "family", "ext", "sha256", "b64"}}]
# — injecté au DÉPLOIEMENT par app/fonts.resolve_params() (seules les polices RÉELLEMENT
# référencées par les params voyagent, en base64). Rootfs ÉPHÉMÈRE → rematérialisation à CHAQUE
# démarrage du script dans un répertoire de travail recréé, puis enregistrement dans
# _FONT_FILES → _font() la sert comme une police d'image. Police illisible/clé inconnue →
# repli DejaVu + log, jamais de crash.
_FONT_LIB_DIR = "/tmp/avs_fonts"

def _materialize_font_library():
    lib = CONFIG.get("font_library") or []
    if not lib:
        return
    try:
        os.makedirs(_FONT_LIB_DIR, exist_ok=True)
    except OSError as e:
        log("[fonts] répertoire de travail impossible (%s) → repli DejaVu" % e, "warning")
        return
    ok = 0
    for ent in lib:
        if not isinstance(ent, dict):
            continue
        key = str(ent.get("key") or "")
        if not key.startswith("lib:"):
            continue
        ext = (ent.get("ext") or "ttf").lower()
        if ext not in ("ttf", "otf", "ttc"):
            ext = "ttf"
        try:
            data = base64.b64decode(ent.get("b64") or "", validate=True)
            if not data:
                raise ValueError("charge utile vide")
            path = os.path.join(_FONT_LIB_DIR, "%s.%s" % (key[4:], ext))
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            ImageFont.truetype(path, 16)          # chargement RÉEL : une police cassée est écartée ici
            _FONT_FILES[key] = [path]
            ok += 1
        except Exception as e:
            log("[fonts] police %s (%s) illisible : %s → repli DejaVu"
                % (key, ent.get("name") or "?", e), "warning")
    log("[fonts] bibliothèque matérialisée : %d/%d police(s) dans %s"
        % (ok, len(lib), _FONT_LIB_DIR), "info")

_materialize_font_library()

# Police choisie (Réglages du container, champ "font" — clé builtin ou "lib:<sha16>") : champ
# scope=user structurel → tout changement redéploie (cf. hooks.py _FORMAT_KEYS), seul moyen
# d'embarquer les octets d'une nouvelle police de bibliothèque (rootfs éphémère).
FONT_KEY = str(CONFIG.get("font") or "dejavu-sans-bold")

def _font(px):
    px = max(8, int(px))
    for path in _FONT_FILES.get(FONT_KEY, []):
        try:
            return ImageFont.truetype(path, px)
        except Exception:
            pass
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(path, px)
        except Exception:
            pass
    return ImageFont.load_default()

FONT_BIG = _font(HEIGHT // 5)
FONT_MED = _font(max(14, HEIGHT // 26))
def _fit_font(sample, start_px, max_w):
    """Plus grande police (≤ start_px) dont `sample` tient dans max_w px."""
    px = start_px
    while px > 12:
        f = _font(px)
        try:
            bb = _probe.textbbox((0, 0), sample, font=f)
            if bb[2] - bb[0] <= max_w:
                return f
        except Exception:
            return f
        px = int(px * 0.9)
    return _font(12)
_probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
def _tsize(txt, font):
    try:
        bb = _probe.textbbox((0, 0), txt, font=font)
        return bb[2] - bb[0], bb[3] - bb[1]
    except Exception:
        return (len(txt) * 8, 12)

# Timecode : REMPLACE le compteur quand le générateur est actif (pas de doublon de chiffres).
# Police ajustée au boot pour que hh:mm:ss:img tienne dans 90 % de la largeur.
FONT_TC = _fit_font("00:00:00:00", HEIGHT // 5, int(WIDTH * 0.9))


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
        log("logo illisible: %s" % e, "warning")
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

def _base_planes(img):
    """Image PIL RGB → payload planar Y|Cb|Cr précalculé (np.uint8 plat, V_FRAME_SIZE octets).
    Conversion float PLEINE trame faite UNE FOIS (boot / changement d'overlay), plus jamais
    par-frame — c'était le hotspot (~68 ms/frame @1080p, fps plafonné ~12)."""
    return np.frombuffer(_rgb_to_yuv420(np.asarray(img)), dtype=np.uint8).copy()

_b, _w = _build_imgs(SHOW_BRAND, CUSTOM_TEXT)
YUV_BLACK, YUV_WHITE = _base_planes(_b), _base_planes(_w)
del _b, _w


def _blend_region(plane, y0, y1, x0, x1, alpha, cval):
    """plane[y0:y1, x0:x1] ← alpha·cval + (1−alpha)·fond — blend entier exact (÷255 via
    *257>>16, astuce memory-bound multiview) sur une PETITE région (le bbox du compteur)."""
    bg = plane[y0:y1, x0:x1].astype(np.uint32)
    a = alpha.astype(np.uint32)
    plane[y0:y1, x0:x1] = (((a * cval + (255 - a) * bg + 128) * 257) >> 16).astype(plane.dtype)


def _render_into(vw, flash, sec, fis, bar_x, yuv_black=None, yuv_white=None, tc_txt=None):
    """Rend la mire DIRECTEMENT dans le grain MXL (zéro-copie) :
    1 memcpy du fond précalculé + patch de la barre (vues stridées, pas de gather) + blend du
    compteur en YUV sur son seul bbox (masque alpha PIL petit format). Sortie visuellement
    identique au chemin historique (écart ≤ 2 codes sur les bords anti-aliasés)."""
    base = (yuv_white if flash else yuv_black)
    if base is None:
        base = YUV_WHITE if flash else YUV_BLACK
    bobimxl.blit(vw, base)
    Y = vw[:Y_SIZE].view(_NP_DT).reshape(HEIGHT, WIDTH)
    U = vw[Y_SIZE:Y_SIZE + UV_SIZE].view(_NP_DT).reshape(UV_H, UV_W)
    V = vw[Y_SIZE + UV_SIZE:V_FRAME_SIZE].view(_NP_DT).reshape(UV_H, UV_W)
    # barre de balayage verticale (PIL rectangle incluait le bord droit → largeur bw+1)
    bw = max(2, WIDTH // 200)
    x1 = min(WIDTH, bar_x + bw + 1)
    Y[:, bar_x:x1] = BAR_YUV[0]
    # chroma : règle historique du sous-échantillonnage = échantillon (i,j) ← pixel haut-gauche
    cx0 = (bar_x + _CW - 1) // _CW; cx1 = (x1 + _CW - 1) // _CW
    U[:, cx0:cx1] = BAR_YUV[1]; V[:, cx0:cx1] = BAR_YUV[2]
    # compteur centré sec.frame : masque alpha rendu sur une petite image L, blend local
    # (helper partagé avec le timecode incrusté — mêmes opérations, octet-identique)
    def _blend_text(txt, font, px, py):
        bb = _probe.textbbox((0, 0), txt, font=font)
        mask = Image.new("L", (bb[2] + 2, bb[3] + 2), 0)
        ImageDraw.Draw(mask).text((0, 0), txt, font=font, fill=255)
        a = np.asarray(mask)
        gx0 = max(0, px); gy0 = max(0, py)
        gx0 -= gx0 % _CW; gy0 -= gy0 % _CH             # région alignée chroma
        gx1 = min(WIDTH, px + a.shape[1]);  gx1 = min(WIDTH, gx1 + (-gx1) % _CW)
        gy1 = min(HEIGHT, py + a.shape[0]); gy1 = min(HEIGHT, gy1 + (-gy1) % _CH)
        if gx1 <= gx0 or gy1 <= gy0:
            return
        reg = np.zeros((gy1 - gy0, gx1 - gx0), dtype=np.uint8)
        y_lo = max(gy0, py); y_hi = min(gy1, py + a.shape[0])
        x_lo = max(gx0, px); x_hi = min(gx1, px + a.shape[1])
        if y_hi > y_lo and x_hi > x_lo:
            reg[y_lo - gy0:y_hi - gy0, x_lo - gx0:x_hi - gx0] = a[y_lo - py:y_hi - py,
                                                                  x_lo - px:x_hi - px]
        cy, cu, cv = TXT_YUV_ON_FLASH if flash else TXT_YUV_ON_BLACK
        _blend_region(Y, gy0, gy1, gx0, gx1, reg, cy)
        ac = reg[::_CH, ::_CW]
        _blend_region(U, gy0 // _CH, gy1 // _CH, gx0 // _CW, gx1 // _CW, ac, cu)
        _blend_region(V, gy0 // _CH, gy1 // _CH, gx0 // _CW, gx1 // _CW, ac, cv)

    # Générateur de timecode ACTIF → le TC (hh:mm:ss:img, même valeur que le grain ANC émis à
    # cet index) REMPLACE le compteur sec.frame (pas de doublon de chiffres à l'image).
    if tc_txt:
        tb = _probe.textbbox((0, 0), tc_txt, font=FONT_TC)
        ttw, tth = tb[2] - tb[0], tb[3] - tb[1]
        _blend_text(tc_txt, FONT_TC, (WIDTH - ttw) // 2, (HEIGHT - tth) // 2)
    else:
        txt = "%d.%02d" % (sec, fis)
        bb = _probe.textbbox((0, 0), txt, font=FONT_BIG)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        _blend_text(txt, FONT_BIG, (WIDTH - tw) // 2, (HEIGHT - th) // 2)


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
    "show_brand": SHOW_BRAND, "custom_text": CUSTOM_TEXT, "tc_enable": TC_ENABLE,
    "beep": BEEP, "beat_ms": BEAT_MS, "yuv_black": YUV_BLACK, "yuv_white": YUV_WHITE,
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
            _live["yuv_black"] = _base_planes(blk); _live["yuv_white"] = _base_planes(wht)


# ─── threads de génération ───────────────────────────────────────────────────
def video_feeder():
    # MODE TRANCHE : le flowDef porte slice_height quand éligible (progressif + divisible) →
    # grain publié en N tranches égales ; sinon flowDef inchangé (historique octet-identique).
    w = bobimxl.Writer(inst, VIDEO_SHM, WIDTH, HEIGHT, CHROMA, BIT_DEPTH, FPS_NUM, FPS_DEN,
                       **({{"slice_height": V_SLICE_H}} if V_SLICE_H else {{}}))
    if SLICE_MODE:
        log("slice_mode %s : sortie %s %s" % ("actif" if V_SLICE_H else "INÉLIGIBLE",
            VIDEO_SHM, ("tranchée par %d lignes (%d tranches)" % (V_SLICE_H, _V_TOTAL_SLICES))
            if V_SLICE_H else "(hauteur %d sans diviseur) → chemin historique" % HEIGHT), "info")
    # Générateur de timecode (option, togglable À CHAUD via /config) : flux ANC data
    # `<hostname>_anc` (RFC 8331, un paquet ATC RP 188 par trame), écrit À L'INDEX DU GRAIN
    # VIDÉO (alignement TC↔image parfait en genlock). Writer créé PARESSEUSEMENT à la première
    # activation (retentes throttlées à 1/s en cas d'échec).
    wa = None; _wa_try = 0.0
    idx = 1
    period = FPS_DEN / FPS_NUM
    _clk = time.time if GENLOCK else time.monotonic
    next_t = _grid_next(_clk(), period) if GENLOCK else _clk()
    frame_k = 0
    win_cnt = 0; win_start = time.time(); win_render_ns = 0
    while True:
        if GENLOCK:
            # Coordonnée ET échéance viennent du point unique (bobimxl), sur l'horloge TAI.
            # Le calcul de grille était fait ici à la main, sur CLOCK_REALTIME — qui vaut l'UTC
            # sur un nœud correctement synchronisé, soit 37 s à côté de la grille de tous les
            # autres producteurs. `lookahead=1` : on vise LE créneau suivant, et pas au-delà.
            # ⚠ Un producteur qui alloue `lookahead=L` PUIS DORT jusqu'à l'instant de cette
            # trame consomme sa marge en dormant : il se réveille AU créneau visé, et l'appel
            # suivant repart de la grille courante + L → la tête avance de L créneaux par tour.
            # La cadence est donc DIVISÉE PAR L, par construction. Mesuré le 2026-08-02 avec
            # L=2 : 25,0 fps pile pour 50 demandés, sur trois microarchitectures, avec 8 ms de
            # rendu pour 20 ms de budget — donc pas un plafond de coût, un décalage d'index.
            # Le rattrapage d'un creux d'ordonnancement reste inclus (chaque appel repart de la
            # grille courante, cf. le `max` de bobimxl.next_index).
            frame_k = w.next_index(lookahead=1)
            bobimxl.sleep_until_tai(w.index_time_ns(frame_k))
            now = _clk()
        else:
            now = _clk()
            if now < next_t:
                time.sleep(next_t - now); now = _clk()
        with _live_lock:
            beat_sec = _live["beat_sec"]; flash_ms = _live["flash_ms"]
            yuv_b = _live["yuv_black"];   yuv_w = _live["yuv_white"]
            tc_on = bool(_live["tc_enable"])
        el = (next_t if GENLOCK else now) - T0     # instant de présentation (grille en genlock)
        phase = el % beat_sec
        flash = phase < (flash_ms / 1000.0)
        sec = int(el)
        fis = int((el - sec) * FPS)
        bar_x = int((phase / beat_sec) * (WIDTH - 1))
        # Timecode (option) : HEURE DU JOUR à l'instant de présentation de la trame (grille PTP
        # en genlock, horloge murale sinon) → hh:mm:ss:img. Même valeur incrustée ET émise ANC.
        tc = None; tc_txt = None
        if tc_on and wa is None and time.monotonic() - _wa_try > 1.0:
            _wa_try = time.monotonic()
            try:
                wa = bobimxl.Writer(inst, ANC_SHM, 0, 0,
                                    flow_def=bobimxl.build_data_flow_def(ANC_SHM, FPS_NUM, FPS_DEN))
                log("timecode: flux ANC %s actif (ATC RP 188, RFC 8331)" % ANC_SHM, "info")
            except Exception as e:
                log("timecode: flux ANC indisponible (%s) — incrustation seule" % e, "warning")
        if tc_on:
            _tp = next_t if GENLOCK else time.time()
            # HEURE CIVILE LOCALE : horloge nœud (TAI) − offset TAI→UTC, puis fuseau (TZ_NAME).
            _civ = _tp - TAI_UTC_OFFSET_S
            _lt = time.localtime(_civ)
            _fr = int((_civ % 1.0) * FPS_NUM / FPS_DEN) % FPS
            tc = (_lt.tm_hour, _lt.tm_min, _lt.tm_sec, _fr)
            tc_txt = "%02d:%02d:%02d:%02d" % tc
        _t_r0 = time.monotonic_ns()
        _i, _gi, _vw = w.open_grain(index=(frame_k if GENLOCK else None))
        # Rendu 0.10.0 DIRECT dans le grain (zéro-copie) : memcpy du fond YUV précalculé +
        # patchs barre/compteur — plus de conversion RGB→YUV pleine trame par-frame.
        _render_into(_vw, flash, sec, fis, bar_x, yuv_b, yuv_w, tc_txt=tc_txt)
        if V_SLICE_H:
            # MODE TRANCHE : commit progressif de la trame déjà rendue (bandes 1..N).
            # Même index de grain, même media_ts, même phase flash/bip — seul le commit change.
            _commit_sliced(w, _gi)
        else:
            w.commit(_gi)
        # Grain ANC du timecode, au MÊME index que la vidéo (paquet ATC RP 188, RFC 8331).
        # Cosmétique/diagnostic → ne jamais casser la cadence de la mire si l'écriture échoue.
        if tc_on and wa is not None and tc is not None:
            try:
                _blob = bobimxl.anc_pack_rfc8331(
                    [{{"did": 0x60, "sdid": 0x60, "line": 9, "hori": 0xFFF,
                       "udw": bobimxl.anc_atc_encode(tc[0], tc[1], tc[2], tc[3])}}])
                _ai, _agi, _avw = wa.open_grain(index=(frame_k if GENLOCK else None))
                _src = np.frombuffer(_blob, dtype=np.uint8)
                _n = min(_src.size, _avw.size)
                _avw[:_n] = _src[:_n]
                wa.commit(_agi)
            except Exception:
                pass
        win_render_ns += time.monotonic_ns() - _t_r0
        idx += 1
        win_cnt += 1
        _e = time.time() - win_start
        if _e >= 1.0:
            with metrics_lock:
                metrics["fps"] = round(win_cnt / _e, 1); metrics["frame_index"] = idx
                # own_latency_ms = coût PROPRE rendu+commit par frame (fenêtre glissante).
                # Aucune attente de cadence/horloge dedans (convention capacité orchestrateur ;
                # la mire est un générateur : pas de get_slice amont par construction).
                metrics["own_latency_ms"] = round(win_render_ns / max(1, win_cnt) / 1e6, 2)
            win_cnt = 0; win_start = time.time(); win_render_ns = 0
        next_t += period
        if next_t < now:
            next_t = (_grid_next(now, period) if GENLOCK else now + period)

def audio_feeder():
    aw = bobimxl.AudioWriter(inst, AUDIO_SHM, channels=A_CHANNELS, sample_rate=A_SAMPLE_RATE)
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
                  "anc_shm": ANC_SHM if lv["tc_enable"] else None,   # générateur de timecode
                  "tc_enable": bool(lv["tc_enable"]),
                  "width": WIDTH, "height": HEIGHT, "fps": FPS, "chroma": CHROMA,
                  "format": FORMAT_STR,
                  "beat_sec": lv["beat_sec"], "flash_ms": lv["flash_ms"],
                  "tone_hz": lv["tone_hz"], "show_brand": lv["show_brand"],
                  "custom_text": lv["custom_text"], "font": FONT_KEY,
                  "brand_system_name": BRAND_NAME, "brand_location": BRAND_LOC,
                  "has_logo": LOGO is not None,
                  "fps_live": m.get("fps"), "frame_index": m.get("frame_index"),
                  "log_level": LOG_LEVEL,   # lisible en condition de macro
                  "plugin_version": PLUGIN_VERSION,
                  "mxl_lib": _mxl_lib_state()}}
            self._send_json(200, s)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path not in ("/config", "/log_level"):
            self.send_response(404); self.end_headers(); return
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n)) if n else {{}}
        except Exception:
            self._send_json(400, {{"error": "JSON invalide"}}); return
        if self.path == "/log_level":
            # Verbosité À CHAUD (pas de redéploiement) : instruction d'incident. Le niveau
            # PERSISTANT reste le champ `log_level` du config_schema ; celui-ci est volatil.
            ok = set_log_level(body.get("level") or body.get("log_level"))
            self._send_json(200 if ok else 400, {{"ok": ok, "log_level": LOG_LEVEL}}); return
        _validators = {{
            "beat_sec": lambda v: float(v),
            "flash_ms": lambda v: int(v),
            "tone_hz":  lambda v: float(v),
            "show_brand": lambda v: bool(v),
            "custom_text": lambda v: str(v),
            "tc_enable": lambda v: bool(v),
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
