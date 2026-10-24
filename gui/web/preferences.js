/* Shared UI preferences. The media player, grid and shell consult one policy. */
(() => {
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)')
  let prefs = { motion: 'system', mediaAutoplay: true, galleryAutoplay: true }
  try { Object.assign(prefs, JSON.parse(localStorage.getItem('douyin-downloader-interaction') || '{}')) } catch {}
  function motionAllowed() { return prefs.motion !== 'reduced' && !reduced.matches }
  function sync() {
    document.documentElement.dataset.motion = motionAllowed() ? 'full' : 'reduced'
    window.dispatchEvent(new CustomEvent('app-preferences-changed'))
  }
  function apply(values) {
    for (const key of Object.keys(prefs)) if (values[key] !== undefined) prefs[key] = values[key]
    try { localStorage.setItem('douyin-downloader-interaction', JSON.stringify(prefs)) } catch {}
    sync()
  }
  reduced.addEventListener('change', sync)
  window.AppPreferences = { apply, motionAllowed, mediaAutoplay: () => prefs.mediaAutoplay !== false,
    galleryAutoplay: () => prefs.galleryAutoplay !== false }
  sync()
})()
