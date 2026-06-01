_FORMAT_KEYS = frozenset(("width", "height", "fps", "chroma"))
_SOFT_KEYS   = frozenset(("beat_sec", "flash_ms", "tone_hz", "show_brand", "custom_text"))


def control_action(action, body, params, ctx):
    """POST /api/containers/<vmid>/control/config.
    Params format (width/height/fps/chroma) → redéploiement.
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
