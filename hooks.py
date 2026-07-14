# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

_FORMAT_KEYS = frozenset(("width", "height", "fps", "chroma", "font"))
_SOFT_KEYS   = frozenset(("beat_sec", "flash_ms", "tone_hz", "show_brand", "custom_text",
                          "tc_enable"))


def before_deploy(params, context):
    """Injecte l'HEURE CIVILE pour le générateur de timecode : fuseau du contrôleur (`tz` —
    les images runtime sont en UTC) + `tai_utc_offset_s` (l'horloge des nœuds est disciplinée
    sur l'échelle PTP/TAI par conception, cf. PTP_CLOCK.md : l'heure civile = horloge nœud −
    offset, MESURÉ contre le contrôleur NTP — jamais 37 figé, robuste aux leap seconds).
    Ré-évalué à CHAQUE déploiement (mesure fraîche, cache 1 h par nœud)."""
    try:
        from app.ptp import civil_clock_params
        params.update(civil_clock_params(context.get("vmid")))
    except Exception:
        pass
    return params


def control_action(action, body, params, ctx):
    """POST /api/containers/<vmid>/control/config.
    Params format (width/height/fps/chroma/font) → redéploiement (la police, comme le format,
    a besoin de recréer le conteneur — c'est le seul moyen d'embarquer les octets d'une police
    de bibliothèque, cf. app/fonts.py : rootfs éphémère, rien n'est jamais persisté en DB).
    Params soft (beat/flash/tone/texte/marque) → appliqués à chaud via :8082/config."""
    if action != "config":
        return None
    new_params = dict(params)
    needs_redeploy = any(k in body for k in _FORMAT_KEYS)
    hot_body = {k: body[k] for k in _SOFT_KEYS if k in body}
    for k in body:
        if k in _FORMAT_KEYS | _SOFT_KEYS:
            new_params[k] = body[k]
    if needs_redeploy or not hot_body:
        return {"params": new_params, "hot_endpoint": None, "hot_body": None}
    return {"params": new_params, "hot_endpoint": "/config", "hot_body": hot_body}
