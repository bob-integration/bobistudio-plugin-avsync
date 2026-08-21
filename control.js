// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Auteur : Cyril Mazouer, pour le compte de BOBI SAS
// Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

// Carte de contrôle avsync — état live + config inline (I/O Sources).
window.MXLPlugins = window.MXLPlugins || {};
window.MXLPlugins["avsync"] = {
  _timers: {},

  mount(el, vmid) {
    const $ = s => el.querySelector(s);
    const esc = s => String(s == null ? '' : s)
      .replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

    // ─── i18n (catalogue plugin.avsync.*, servi par l'orchestrateur) ─────────
    // Le fragment HTML porte le FRANÇAIS en dur comme repli : si le catalogue manque, on lit du
    // français, jamais une clé brute. On traduit le texte ET les attributs (title/aria/placeholder)
    // — un libellé traduit au-dessus d'une infobulle française serait un demi-travail.
    // `window.t` rend la CLÉ BRUTE quand elle est absente du catalogue (layout.html). Écrire ce
    // retour sans le tester REMPLACERAIT le français du fragment par « plugin.avsync.beat » — le
    // repli en dur ne servirait alors QUE le cas improbable où window.t n'existe pas, et raterait
    // le cas réel (clé perdue/pas encore semée). D'où le test explicite : clé non résolue → on
    // GARDE le texte déjà en place.
    const T = (k, repli) => {
      const v = window.t ? window.t(k) : k;
      return (v && v !== k) ? v : (repli !== undefined ? repli : k);
    };
    const appliquerI18n = (root) => {
      if (!window.t) return;
      const attr = (sel, ds, lire, ecrire) => root.querySelectorAll(sel).forEach(n => {
        const v = T(n.dataset[ds], lire(n));
        if (v != null) ecrire(n, v);
      });
      attr('[data-i18n]',       'i18n',      n => n.textContent,                (n, v) => { n.textContent = v; });
      attr('[data-i18n-title]', 'i18nTitle', n => n.title,                      (n, v) => { n.title = v; });
      attr('[data-i18n-aria]',  'i18nAria',  n => n.getAttribute('aria-label'), (n, v) => n.setAttribute('aria-label', v));
      attr('[data-i18n-ph]',    'i18nPh',    n => n.placeholder,                (n, v) => { n.placeholder = v; });
    };
    appliquerI18n(el);

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

      // Sélectionner le format courant. Aucun preset ne matche (format hors palette) →
      // option synthétique « (actuel) » sélectionnée, plutôt que de laisser le select sur la
      // PREMIÈRE entrée de la palette (qui affichait un format mensonger, ex. 1080i50 pour
      // une mire 720p25).
      if (currentW && currentH && currentFps) {
        const idx = _formats.findIndex(f => f.w === currentW && f.h === currentH
          && Math.round(f.fps) === Math.round(currentFps));
        if (idx >= 0) presetSel.value = String(idx);
        else {
          presetSel.insertAdjacentHTML('afterbegin',
            `<option value="cur">${esc(`${currentW}×${currentH} @${currentFps} (actuel)`)}</option>`);
          presetSel.value = 'cur';
        }
      }
    }

    // Synchro chroma quand le preset change
    el.querySelector('[name="fmt_preset"]')?.addEventListener('change', e => {
      const f = _formats[+e.target.value];
      if (!f) return;
      const chromaSel = el.querySelector('[name="chroma"]');
      if (chromaSel) chromaSel.value = f.chroma;
      _dirty.add('fmt_preset'); _dirty.add('chroma');   // choix utilisateur → protégé du refresh
    });

    // ─── Form fill (refresh périodique SANS écraser l'édition en cours) ──────
    // Champs « dirty » = modifiés par l'utilisateur depuis le dernier Appliquer :
    // le polling ne les réécrit JAMAIS (ni un champ qui a le focus). L'Appliquer
    // d'un groupe ré-autorise la synchro de ses seuls champs.
    const _dirty = new Set();
    const GROUP_FIELDS = {
      fmt:  ['fmt_preset', 'chroma'],
      av:   [...el.querySelectorAll('.avs-knob[data-name]')].map(k => k.dataset.name),
      disp: ['show_brand', 'custom_text', 'tc_enable', 'probe_bands_on'],
      font: ['font'],
    };
    let _fmtLoaded = false;

    // ─── Sélecteur de police (Réglages → Polices, bibliothèque incluse) ───────
    // Alimenté par le helper partagé window.BobiFonts (groupes « Polices système » /
    // « Bibliothèque ») — même motif que le composer multiview. Peuplé une seule fois
    // (resynchronisé après un Appliquer du groupe font, comme fmt_preset).
    let _fontLoaded = false;
    function loadFontSelect(current) {
      const sel = el.querySelector('[name="font"]');
      if (!sel || !window.BobiFonts) return;
      window.BobiFonts.load().then(() => {
        window.BobiFonts.fillSelect(sel, current || 'dejavu-sans-bold');
      }).catch(() => { /* catalogue indisponible : select laissé vide, retenté au refresh */ });
    }

    function fillForm(s) {
      const set = (name, val) => {
        const f = el.querySelector(`[name="${name}"]`);
        if (!f || _dirty.has(name) || document.activeElement === f) return;
        // Clé ABSENTE du /state (script plus ancien que l'UI) : ne pas resynchroniser le champ
        // — sinon un toggle appliqué « ok » se décoche tout seul au polling suivant.
        if (val === undefined) return;
        if (f.type === 'checkbox') f.checked = Boolean(val);
        else f.value = val != null ? val : '';
      };
      // Peuple le select depuis les réglages + sélectionne le format courant (1 seule fois,
      // resynchronisé après un Appliquer du groupe fmt)
      if (!_fmtLoaded) {
        _fmtLoaded = true;
        loadFmtSelect(s.width, s.height, s.fps).then(() => {
          set('chroma', s.chroma || '422');
        });
      }
      // Rotatifs (jamais pendant un drag ni s'ils sont dirty)
      el.querySelectorAll('.avs-knob[data-name]').forEach(k => {
        if (_dirty.has(k.dataset.name) || (_drag && _drag.k === k)) return;
        const v = s[k.dataset.name];
        if (v != null) _setKnob(k, v);
      });
      set('show_brand', s.show_brand);
      set('custom_text',s.custom_text);
      set('tc_enable',  s.tc_enable);
      set('probe_bands_on', (s.probe_bands || 0) > 0);
      if (!_fontLoaded && !_dirty.has('font')) {
        _fontLoaded = true;
        loadFontSelect(s.font);
      }
    }

    // toute saisie utilisateur marque le champ dirty (protégé du refresh jusqu'à Appliquer)
    el.querySelectorAll('input, select').forEach(f => {
      const mark = () => { if (f.name) _dirty.add(f.name); };
      f.addEventListener('input', mark);
      f.addEventListener('change', mark);
    });

    // ─── Polling ─────────────────────────────────────────────────────────────
    async function refresh() {
      try {
        const s = await (await fetch(`/api/containers/${vmid}/plugin/state`)).json();
        updateLive(s);
        fillForm(s);
      } catch {
        if (fpsEl) fpsEl.textContent = '—';
        if (fmtEl) fmtEl.textContent = T('plugin.avsync.stopped');
      }
    }

    refresh();
    if (this._timers[vmid]) clearInterval(this._timers[vmid]);
    this._timers[vmid] = setInterval(refresh, 3000);

    // ─── Rotatifs ────────────────────────────────────────────────────────────
    // Drag vertical : haut = augmente. Le TRACÉ est celui du catalogue (arc).

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
      // Tracé délégué au catalogue : arc + graduations + repère de la valeur par défaut.
      // Seule la zone de préhension est réécrite — le bouton de remise à zéro, qui est un
      // frère dans le rotatif, survit donc à chaque mise à jour.
      const dial = knob.querySelector('.ctl-knob-hit');
      if (dial) {
        const def = parseFloat(knob.dataset.default);
        dial.innerHTML = window.MXLControls.knobSvg('arc', (val - min) / (max - min),
          isFinite(def) ? (def - min) / (max - min) : null);
        dial.setAttribute('aria-valuenow', val);
      }
      const valEl = knob.querySelector('.avs-knob-val');
      if (valEl) {
        const unitEl = valEl.querySelector('.avs-knob-unit');
        const unit   = unitEl ? unitEl.outerHTML : '';
        valEl.innerHTML = val + unit;
      }
      return val;
    }

    // Application à chaud, avec anti-rafale. Un seul point d'entrée pour les deux sources :
    // les dials (widgets maison, pas d'événement `change`) et les champs natifs.
    const _tempo = {};
    function _appliquerAChaud(group, delai) {
      clearTimeout(_tempo[group]);
      _tempo[group] = setTimeout(() => soumettre(group, null), delai || 0);
    }

    // Initialiser les dials au chargement
    el.querySelectorAll('.avs-knob').forEach(k =>
      _setKnob(k, parseFloat(k.dataset.val)));

    // Remise à la valeur par défaut. Le repère sur l'arc dit OÙ est le défaut ; ce bouton y
    // ramène. Sans lui, revenir au réglage d'origine demande de se souvenir du chiffre.
    el.addEventListener('click', e => {
      const b = e.target.closest('.ctl-knob-reset');
      if (!b) return;
      const k = b.closest('.avs-knob');
      const def = k && parseFloat(k.dataset.default);
      if (!k || !isFinite(def)) return;
      _setKnob(k, def);
      if (k.dataset.name) _dirty.add(k.dataset.name);
      _appliquerAChaud('av');
    });

    // Drag
    let _drag = null;
    el.addEventListener('pointerdown', e => {
      if (e.target.closest('.ctl-knob-reset')) return;   // le reset n'est pas une prise
      const k = e.target.closest('.avs-knob');
      if (!k) return;
      const min = parseFloat(k.dataset.min), max = parseFloat(k.dataset.max);
      _drag = { k, startY: e.clientY, startVal: parseFloat(k.dataset.val),
                sens: (max - min) / 150 };
      try { k.setPointerCapture(e.pointerId); } catch (_) {}
      // preventDefault empêche la sélection de texte pendant le glissé, mais il empêche AUSSI
      // le clic de donner le focus — donc ni le cercle de sélection ni le bouton de remise à
      // zéro, qui n'apparaît qu'au focus, ne se montraient. On focalise donc à la main.
      const hit = k.querySelector('.ctl-knob-hit');
      if (hit) hit.focus();
      e.preventDefault();
    }, { passive: false });

    el.addEventListener('pointermove', e => {
      if (!_drag) return;
      const raw = _drag.startVal + (_drag.startY - e.clientY) * _drag.sens;
      _setKnob(_drag.k, raw);
      if (_drag.k.dataset.name) _dirty.add(_drag.k.dataset.name);
    });

    el.addEventListener('pointerup', e => {
      if (!_drag) return;
      try { _drag.k.releasePointerCapture(e.pointerId); } catch (_) {}
      _drag = null;
      // Les dials sont des widgets maison : ils n'émettent PAS d'événement `change`, l'écoute
      // déléguée plus bas ne les verrait donc jamais. On applique à la FIN du geste — pas
      // pendant le glissé, qui enverrait cent requêtes pour un quart de tour.
      _appliquerAChaud('av');
    });

    // Molette
    el.addEventListener('wheel', e => {
      const k = e.target.closest('.avs-knob');
      if (!k) return;
      e.preventDefault();
      const step = _knobStep(k) * (e.shiftKey ? 5 : 1);
      _setKnob(k, parseFloat(k.dataset.val) + (e.deltaY < 0 ? step : -step));
      if (k.dataset.name) _dirty.add(k.dataset.name);
      _appliquerAChaud('av', 400);   // rafale de molette → on applique quand elle s'arrête
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
      if (k.dataset.name) _dirty.add(k.dataset.name);
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
      if (group === 'font') {
        const sel = el.querySelector('[name="font"]');
        return sel && sel.value ? { font: sel.value } : {};
      }
      // disp
      const body = {};
      const chk = el.querySelector('[name="show_brand"]');
      if (chk) body.show_brand = chk.checked;
      const txt = el.querySelector('[name="custom_text"]');
      if (txt) body.custom_text = txt.value;
      const tce = el.querySelector('[name="tc_enable"]');
      if (tce) body.tc_enable = tce.checked;
      // Bandeaux-sonde : un simple ON/OFF. Le NOMBRE de bandeaux est un détail
      // d'expert (il reste dans le config_schema) — l'opérateur n'a qu'une question à se
      // poser : est-ce que je mesure, oui ou non. 8 est la valeur qui convient partout.
      const pbo = el.querySelector('[name="probe_bands_on"]');
      if (pbo) body.probe_bands = pbo.checked ? 8 : 0;
      return body;
    }

    function _clearDirty(group) {
      (GROUP_FIELDS[group] || []).forEach(n => _dirty.delete(n));
      if (group === 'fmt') _fmtLoaded = false;   // resélectionner le preset au prochain refresh
      if (group === 'font') _fontLoaded = false; // resynchro la police au prochain refresh
    }

    function feedback(group, msg, ok) {
      const fb = el.querySelector(`.avs-fb[data-group="${group}"]`);
      if (!fb) return;
      fb.textContent = msg;
      fb.className = `avs-fb ${ok ? 'ok' : 'err'}`;
      setTimeout(() => { fb.textContent = ''; fb.className = 'avs-fb'; }, 3500);
    }

    // ─── Soumission ──────────────────────────────────────────────────────────
    // ─── Application ──────────────────────────────────────────────────────────
    // DEUX RÉGIMES, et il ne faut surtout pas les confondre derrière un bouton unique :
    //   · réglages À CHAUD (timing A/V, affichage) → AUCUN bouton, appliqués au `change`.
    //     Un réglage qui n'a pas d'effet tant qu'on n'a pas cliqué ailleurs invite à l'erreur :
    //     l'opérateur tourne un bouton, ne voit rien bouger, et recommence.
    //   · réglages STRUCTURELS (format, police) → bouton EXPLICITE, qui dit qu'il redéploie.
    //     Recréer le conteneur coupe un signal peut-être à l'antenne : le clic délibéré est une
    //     confirmation, pas une gêne. C'est précisément ce qu'un « Appliquer » global aurait
    //     effacé, en mettant un demi-décibel et une recréation de conteneur au même prix.
    async function soumettre(group, ctrl) {
      const body = collect(group);
      if (!Object.keys(body).length) return;
      if (ctrl) ctrl.disabled = true;
      try {
        const r = await fetch(`/api/containers/${vmid}/control/config`, {
          method:  'POST',
          headers: {'Content-Type': 'application/json'},
          body:    JSON.stringify(body),
        });
        const res = await r.json();
        if (!r.ok) {
          feedback(group, res.error || `erreur ${r.status}`, false);
        } else if (res.status === 'deploiement_en_cours') {
          feedback(group, T('plugin.avsync.redeploying'), true);
          _clearDirty(group);
        } else {
          feedback(group, 'ok', true);
          _clearDirty(group);   // ré-autoriser la synchro des champs appliqués
        }
      } catch {
        feedback(group, T('plugin.avsync.network'), false);
      } finally {
        if (ctrl) ctrl.disabled = false;
      }
    }

    // Structurels : le bouton reste, et son libellé dit la conséquence.
    el.querySelectorAll('.avs-apply').forEach(btn => {
      btn.addEventListener('click', () => soumettre(btn.dataset.group, btn));
    });

    // À chaud : application au `change`, sans bouton. `change` et non `input` — on applique
    // quand la valeur est POSÉE (molette relâchée, champ quitté), pas à chaque frappe, sinon
    // un texte de huit lettres partirait huit fois.
    const _GROUPE_A_CHAUD = {av: 'av', disp: 'disp'};
    el.addEventListener('change', (ev) => {
      const t = ev.target;
      if (!t || !t.closest) return;
      if (t.closest('.avs-apply')) return;
      const sec = t.closest('.avs-sec');
      if (!sec) return;
      const fb = sec.querySelector('.avs-fb[data-group]');
      const g = fb && fb.dataset.group;
      if (g && _GROUPE_A_CHAUD[g]) soumettre(g, null);
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
