// Carte de contrôle avsync. Enregistrée sur window.MXLPlugins["avsync"].
// mount(el, vmid, ctx) est appelée par le shell Sources après injection de control.html.
// avsync n'expose que /state (lecture) sur :8082 → carte informative + monitoring.
window.MXLPlugins = window.MXLPlugins || {};
window.MXLPlugins["avsync"] = {
  _timers: {},
  mount(el, vmid, ctx){
    const inst = (ctx && ctx.instance) || {};
    const $ = s => el.querySelector(s);
    const stateEl = $('.avs-state');

    const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[c]));
    const row = (k, v) => `<div class="avs-row"><span>${esc(k)}</span><span>${esc(v)}</span></div>`;

    async function refresh(){
      try {
        const s = await (await fetch(`/api/containers/${vmid}/plugin/state`)).json();
        const fmt = s.format || ((s.width && s.height) ? `${s.width}×${s.height}p${s.fps}` : '—');
        const live = (s.fps_live != null) ? s.fps_live : '—';
        const brand = s.show_brand ? (s.brand_system_name || 'activée') : 'désactivée';
        stateEl.innerHTML =
          row('Format', fmt) +
          row('FPS live', live) +
          row('Battement', (s.beat_sec != null ? s.beat_sec + ' s' : '—')) +
          row('Tonalité', (s.tone_hz != null ? s.tone_hz + ' Hz' : '—')) +
          row('SHM vidéo', s.video_shm || '—') +
          row('Marque', brand) +
          row('Version', 'v' + (s.plugin_version || '?'));
      } catch(e) {
        stateEl.innerHTML = '<div class="avs-row meta">état indisponible (container arrêté ?)</div>';
      }
    }

    refresh();
    if (this._timers[vmid]) clearInterval(this._timers[vmid]);
    this._timers[vmid] = setInterval(refresh, 3000);
  },
  unmount(vmid){
    if (vmid != null) { clearInterval(this._timers[vmid]); delete this._timers[vmid]; }
    else { Object.values(this._timers).forEach(clearInterval); this._timers = {}; }
  }
};
