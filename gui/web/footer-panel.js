// Footer panel: one floating frame above the status bar shared by the scan
// and download views.  Hovering a footer lane opens that lane's view; moving
// to the other lane while the frame is up slides the frame over, eases its
// height to the new view and crossfades the two views, so two panels never
// stack on top of each other.  The frame stays open while the pointer is
// over a lane or the frame, and closes shortly after it leaves both.
(() => {
  const HIDE_DELAY_MS = 200
  // Matches the longest .footer-panel / .fp-view transition in style.css.
  const ANIMATION_MS = 260
  // Gap between the frame and the footer, bridged by the arrow.
  const ARROW_GAP = 10
  const views = new Map()
  const viewTimers = new Map()
  let active = ''
  let open = false
  let hideTimer = null
  let closeTimer = null
  let resizeObserver = null

  const shell = () => document.getElementById('footer-panel')
  const body = () => shell()?.querySelector('.fp-body')

  // Centered on the lane's text; near a window edge the frame shifts but the
  // arrow keeps pointing at the text.
  function position() {
    const view = views.get(active)
    const node = shell()
    if (!view || !node) return
    const lane = view.lane()
    const anchor = view.anchor() || lane
    if (!anchor || !lane) return
    const text = anchor.getBoundingClientRect()
    const footer = lane.getBoundingClientRect()
    const width = Math.min(680, window.innerWidth - 24)
    const center = text.left + text.width / 2
    const left = Math.max(12, Math.min(center - width / 2, window.innerWidth - width - 12))
    node.style.width = `${width}px`
    node.style.left = `${Math.round(left)}px`
    node.style.bottom = `${Math.max(0, window.innerHeight - footer.top) + ARROW_GAP}px`
    node.style.setProperty('--arrow-x', `${Math.round(Math.max(18, Math.min(width - 18, center - Math.round(left))))}px`)
  }

  // The views are absolutely positioned, so the body's height is set from the
  // active view and eases whenever that view grows or shrinks.
  function fitHeight() {
    const view = views.get(active)
    const node = body()
    if (view && node && !shell().hidden) node.style.height = `${view.view().offsetHeight}px`
  }

  function setActive(name, instant) {
    const previous = active
    active = name
    for (const [key, view] of views) {
      const element = view.view()
      clearTimeout(viewTimers.get(key))
      if (key === name) {
        if (element.hidden) { element.hidden = false; void element.offsetWidth }
        element.classList.add('active')
      } else {
        element.classList.remove('active')
        if (instant) element.hidden = true
        else viewTimers.set(key, setTimeout(() => { if (active !== key) element.hidden = true }, ANIMATION_MS))
      }
    }
    if (previous && previous !== name) views.get(previous).deactivate()
    views.get(name).activate()
  }

  function show(name) {
    const view = views.get(name)
    const node = shell()
    if (!view || !node || !view.available() || window.AppUI?.activeModal()) return
    clearTimeout(hideTimer)
    if (open && active === name) return
    clearTimeout(closeTimer)
    // Opening from closed places the frame without sliding or resizing; only
    // the rise-and-fade runs.  Re-entering during the fade-out, or switching
    // lanes, animates from wherever the frame is.
    const fresh = node.hidden
    if (fresh) {
      node.classList.add('fp-instant')
      node.hidden = false
    }
    open = true
    setActive(name, fresh)
    position()
    fitHeight()
    if (fresh) {
      void node.offsetWidth
      node.classList.remove('fp-instant')
    }
    node.classList.add('open')
  }

  function hide(immediate = false) {
    clearTimeout(hideTimer)
    if (!open) return
    open = false
    views.get(active)?.deactivate()
    const node = shell()
    node.classList.remove('open')
    clearTimeout(closeTimer)
    if (immediate) node.hidden = true
    else closeTimer = setTimeout(() => { if (!open) node.hidden = true }, ANIMATION_MS)
  }

  // Closes the frame only while it shows that view.
  function close(name) {
    if (active === name) hide()
  }

  function scheduleHide() {
    clearTimeout(hideTimer)
    hideTimer = setTimeout(hide, HIDE_DELAY_MS)
  }

  // view: { lane(), anchor(), view(), available(), activate(), deactivate() }
  function register(name, view) {
    const node = shell()
    const lane = view.lane()
    if (!node || !lane || views.has(name)) return
    views.set(name, view)
    lane.addEventListener('mouseenter', () => show(name))
    lane.addEventListener('mouseleave', scheduleHide)
    if (!node.dataset.panelBound) {
      node.dataset.panelBound = '1'
      node.addEventListener('mouseenter', () => clearTimeout(hideTimer))
      node.addEventListener('mouseleave', scheduleHide)
      window.addEventListener('resize', () => { if (open) { position(); fitHeight() } })
      resizeObserver = new ResizeObserver(fitHeight)
    }
    resizeObserver.observe(view.view())
  }

  window.FooterPanel = { register, show, hide, close }
})()
