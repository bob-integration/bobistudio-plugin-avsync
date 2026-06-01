// Carte de contrôle avsync — état live + config inline (I/O Sources).
window.MXLPlugins = window.MXLPlugins || {};
window.MXLPlugins["avsync"] = {
  _timers: {},

  mount(el, vmid) {
    const $ = s => el.querySelector(s);
    const esc = s => String(s == null ? '' : s)
      .replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

    // ─── Live state ──────────────────────────────────────────────────────────
    const fpsEl   = $('.avs-fps-val');
    const fmtEl   = $('.avs-live-fmt');
    const brandEl = el.querySelector('[data-live-brand]');

    function updateLive(s) {
      if (!s) return;
      if (fpsEl)  fpsEl.textContent  = s.fps_live != null ? s.fps_live.toFixed(1) : '—';
      if (fmtEl)  fmtEl.textContent  = s.format ? `${s.format} (${s.chroma || '—'})` : '—';

      el.querySelectorAll('[data-live]').forEach(span => {
        const k = span.dataset.live;
        const v = s[k];
        span.textContent = v != null ? v : '—';
      });

      if (brandEl) {
        brandEl.textContent = s.show_brand
          ? `marque ${s.brand_system_name ? esc(s.brand_system_name) : 'on'}`
          : 'marque off';
      }
    }

    // ─── Chargement des formats depuis les réglages ───────────────────────────
    let _formats = [];

    async function loadFmtSelect(currentW, currentH, currentFps) {
      const presetSel = el.querySelector('[name="fmt_preset"]');
      if (!presetSel) return;
      try {
        // Réutilise loadVideoFormats() de scripts.js si disponible
        _formats = (typeof loadVideoFormats === 'function')
          ? await loadVideoFormats()
          : await fetch('/api/settings').then(r => r.json()).then(s => {
              return (s.video_formats || '').split('\n').map(l => l.trim()).filter(Boolean)
                .map(l => {
                  const p = l.split(';').map(x => x.trim());
                  const chroma = ['420','422','444'].includes(p[5]) ? p[5] : '422';
                  return { label: p[0], w: +p[1], h: +p[2], fps: parseFloat(p[3]),
                           scan: p[4] || 'p', chroma };
                }).filter(f => f.label && f.w && f.h);
            });
      } catch (_) { _formats = []; }

      presetSel.innerHTML = _formats.map((f, i) =>
        `<option value="${i}">${esc(f.label)}</option>`
      ).join('');

      // Sélectionner le format courant
      if (currentW && currentH && currentFps) {
        const idx = _formats.findIndex(f => f.w === currentW && f.h === currentH
          && Math.round(f.fps) === Math.round(currentFps));
        if (idx >= 0) presetSel.value = String(idx);
      }
    }

    // Synchro chroma quand le preset change
    el.querySelector('[name="fmt_preset"]')?.addEventListener('change', e => {
      const f = _formats[+e.target.value];
      if (!f) return;
      const chromaSel = el.querySelector('[name="chroma"]');
      if (chromaSel) chromaSel.value = f.chroma;
    });

    // ─── Form fill (unique au premier chargement) ────────────────────────────
    let _filled = false;

    function fillForm(s) {
      if (_filled) return;
      _filled = true;
      const set = (name, val) => {
        const f = el.querySelector(`[name="${name}"]`);
        if (!f) return;
        if (f.type === 'checkbox') f.checked = Boolean(val);
        else f.value = val != null ? val : '';
      };
      // Peuple le select depuis les réglages + sélectionne le format courant
      loadFmtSelect(s.width, s.height, s.fps).then(() => {
        set('chroma', s.chroma || '422');
      });
      // Rotatifs
      el.querySelectorAll('.avs-knob[data-name]').forEach(k => {
        const v = s[k.dataset.name];
        if (v != null) _setKnob(k, v);
      });
      set('show_brand', s.show_brand);
      set('custom_text',s.custom_text);
    }

    // reset flag si l'utilisateur touche un champ
    el.querySelectorAll('input, select').forEach(f =>
      f.addEventListener('input', () => { _filled = false; }));

    // ─── Polling ─────────────────────────────────────────────────────────────
    async function refresh() {
      try {
        const s = await (await fetch(`/api/containers/${vmid}/plugin/state`)).json();
        updateLive(s);
        fillForm(s);
      } catch {
        if (fpsEl) fpsEl.textContent = '—';
        if (fmtEl) fmtEl.textContent = 'container arrêté';
      }
    }

    refresh();
    if (this._timers[vmid]) clearInterval(this._timers[vmid]);
    this._timers[vmid] = setInterval(refresh, 3000);

    // ─── Rotatifs ────────────────────────────────────────────────────────────
    // Course 270° (−135° à +135°). Drag vertical : haut = augmente.
    function _knobAngle(val, min, max) {
      const pct = Math.max(0, Math.min(1, (val - min) / (max - min)));
      return -135 + pct * 270;
    }

    function _knobStep(knob) {
      return parseFloat(knob.dataset.step) || 1;
    }

    function _setKnob(knob, val) {
      const min  = parseFloat(knob.dataset.min);
      const max  = parseFloat(knob.dataset.max);
      const step = _knobStep(knob);
      val = Math.round(val / step) * step;
      val = parseFloat(Math.max(min, Math.min(max, val)).toFixed(10));
      // arrondi propre pour éviter 0.30000000001
      val = parseFloat(val.toPrecision(6));
      knob.dataset.val = val;
      knob.setAttribute('aria-valuenow', val);
      const dial = knob.querySelector('.avs-knob-dial');
      if (dial) dial.style.transform = `rotate(${_knobAngle(val, min, max)}deg)`;
      const valEl = knob.closest('.avs-knob-wrap')?.querySelector('.avs-knob-val');
      if (valEl) {
        const unitEl = valEl.querySelector('.avs-knob-unit');
        const unit   = unitEl ? unitEl.outerHTML : '';
        valEl.innerHTML = val + unit;
      }
      return val;
    }

    // Initialiser les dials au chargement
    el.querySelectorAll('.avs-knob').forEach(k =>
      _setKnob(k, parseFloat(k.dataset.val)));

    // Drag
    let _drag = null;
    el.addEventListener('pointerdown', e => {
      const k = e.target.closest('.avs-knob');
      if (!k) return;
      const min = parseFloat(k.dataset.min), max = parseFloat(k.dataset.max);
      _drag = { k, startY: e.clientY, startVal: parseFloat(k.dataset.val),
                sens: (max - min) / 150 };
      try { k.setPointerCapture(e.pointerId); } catch (_) {}
      e.preventDefault();
    }, { passive: false });

    el.addEventListener('pointermove', e => {
      if (!_drag) return;
      const raw = _drag.startVal + (_drag.startY - e.clientY) * _drag.sens;
      _setKnob(_drag.k, raw);
      _filled = false;
    });

    el.addEventListener('pointerup', e => {
      if (!_drag) return;
      try { _drag.k.releasePointerCapture(e.pointerId); } catch (_) {}
      _drag = null;
    });

    // Molette
    el.addEventListener('wheel', e => {
      const k = e.target.closest('.avs-knob');
      if (!k) return;
      e.preventDefault();
      const step = _knobStep(k) * (e.shiftKey ? 5 : 1);
      _setKnob(k, parseFloat(k.dataset.val) + (e.deltaY < 0 ? step : -step));
      _filled = false;
    }, { passive: false });

    // Clavier (flèches)
    el.addEventListener('keydown', e => {
      const k = e.target.closest('.avs-knob');
      if (!k) return;
      if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) return;
      e.preventDefault();
      const step = _knobStep(k) * (e.shiftKey ? 5 : 1);
      _setKnob(k, parseFloat(k.dataset.val)
        + (['ArrowUp', 'ArrowRight'].includes(e.key) ? step : -step));
      _filled = false;
    });

    // ─── Collecte par groupe ─────────────────────────────────────────────────
    function collect(group) {
      // Format : décompose le preset "WxH@FPS" + chroma
      if (group === 'fmt') {
        const idx = parseInt(el.querySelector('[name="fmt_preset"]')?.value, 10);
        const f = _formats[idx];
        if (!f) return {};
        const body = { width: f.w, height: f.h, fps: f.fps };
        const ch = el.querySelector('[name="chroma"]');
        if (ch) body.chroma = ch.value;
        return body;
      }
      if (group === 'av') {
        // Lire les valeurs depuis les rotatifs
        const body = {};
        el.querySelectorAll('.avs-knob[data-name]').forEach(k => {
          const v = parseFloat(k.dataset.val);
          if (!isNaN(v)) body[k.dataset.name] = v;
        });
        return body;
      }
      // disp
      const body = {};
      const chk = el.querySelector('[name="show_brand"]');
      if (chk) body.show_brand = chk.checked;
      const txt = el.querySelector('[name="custom_text"]');
      if (txt) body.custom_text = txt.value;
      return body;
    }

    function feedback(group, msg, ok) {
      const fb = el.querySelector(`.avs-fb[data-group="${group}"]`);
      if (!fb) return;
      fb.textContent = msg;
      fb.className = `avs-fb ${ok ? 'ok' : 'err'}`;
      setTimeout(() => { fb.textContent = ''; fb.className = 'avs-fb'; }, 3500);
    }

    // ─── Soumission ──────────────────────────────────────────────────────────
    el.querySelectorAll('.avs-apply').forEach(btn => {
      btn.addEventListener('click', async () => {
        const group = btn.dataset.group;
        const body  = collect(group);
        if (!Object.keys(body).length) return;
        btn.disabled = true;
        try {
          const r   = await fetch(`/api/containers/${vmid}/control/config`, {
            method:  'POST',
            headers: {'Content-Type': 'application/json'},
            body:    JSON.stringify(body),
          });
          const res = await r.json();
          if (!r.ok) {
            feedback(group, res.error || `erreur ${r.status}`, false);
          } else if (res.status === 'deploiement_en_cours') {
            feedback(group, 'redéploiement…', true);
            _filled = false;
          } else {
            feedback(group, 'ok', true);
            _filled = false;   // forcer refresh des valeurs live
          }
        } catch {
          feedback(group, 'réseau', false);
        } finally {
          btn.disabled = false;
        }
      });
    });
  },

  unmount(vmid) {
    if (vmid != null) {
      clearInterval(this._timers[vmid]);
      delete this._timers[vmid];
    } else {
      Object.values(this._timers).forEach(clearInterval);
      this._timers = {};
    }
  },
};
