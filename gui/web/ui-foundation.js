/* Small DOM primitives shared by the vanilla-JS screens.
 * Surfaces clip; scroll containers retain their own state; overlays are mounted
 * outside animated page content and use the layer tokens in ui-foundation.css.
 */
(() => {
  const modalSelector = '.following-overlay[role="dialog"], .monitor-delete-overlay [role="dialog"], .image-overlay[role="dialog"], #streamer-modal, .work-detail-dialog[role="dialog"]'
  const pageOverlaySelector = '.following-overlay, .monitor-delete-overlay, .image-overlay, .monitor-row-menu'
  const scrollSelector = '.table-wrap, .following-list-wrap, .column-picker-list, .comment-list, .image-grid, .settings-layout > .settings'
  const locked = new Map()
  const restoredScroll = new WeakMap()
  const modalFocus = new WeakMap()
  let previousModal = null
  let returnFocus = null
  let lastOutsideFocus = null

  function activeModal() {
    // Image previews sit above the player; otherwise the player takes priority.
    const dialogs = [...document.querySelectorAll(modalSelector)]
      .filter(node => !node.hidden && node.getClientRects().length)
    return [...dialogs].reverse().find(node => node.classList.contains('image-overlay'))
      || dialogs.find(node => node.id === 'streamer-modal') || dialogs.at(-1) || null
  }

  function lock(node, value) {
    if (!node) return
    if (value) {
      if (!locked.has(node)) locked.set(node, node.hasAttribute('inert'))
      node.inert = true
    } else if (locked.has(node)) {
      node.inert = locked.get(node)
      locked.delete(node)
    }
  }

  function syncModal() {
    const modal = activeModal()
    document.body.classList.toggle('ui-modal-open', !!modal)
    if (modal && !previousModal) {
      returnFocus = lastOutsideFocus
      window.FooterPanel?.hide(true)
      const tip = document.getElementById('nav-tip')
      if (tip) { tip.hidden = true; tip.classList.remove('show') }
    }
    const candidates = [document.getElementById('app'), document.getElementById('footer-panel'),
      document.getElementById('log-drawer'), document.getElementById('nav-tip'),
      document.getElementById('work-detail-host'), document.getElementById('streamer-modal'),
      ...document.querySelectorAll('.ui-overlay-host > *, body > .monitor-delete-overlay, body > .image-overlay, .monitor-fixed-header')]
    candidates.forEach(node => lock(node, !!modal && node !== modal && !node?.contains(modal)))
    for (const node of locked.keys()) if (!node.isConnected) locked.delete(node)
    if (!modal && previousModal) {
      for (const node of [...locked.keys()]) lock(node, false)
      // Page renders already restore a logical focus target. Standalone dialogs
      // (QR/image/resume) return to their still-connected trigger here.
      if (document.activeElement === document.body && returnFocus?.isConnected
          && !returnFocus.closest('[inert]') && !returnFocus.disabled) {
        returnFocus.focus({ preventScroll: true })
      }
      returnFocus = null
    }
    if (modal && !modal.contains(document.activeElement)) {
      const saved = modalFocus.get(modal)
      const target = saved?.isConnected && modal.contains(saved) && !saved.disabled ? saved
        : modal.querySelector('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]')
      if (target) target.focus({ preventScroll: true })
      else { modal.setAttribute('tabindex', '-1'); modal.focus({ preventScroll: true }) }
    }
    previousModal = modal
    return modal
  }

  function mountPageOverlays(content) {
    const host = document.getElementById('ui-overlay-host')
    if (!host) return
    const nodes = [...content.querySelectorAll(pageOverlaySelector)]
    host.replaceChildren(...nodes)
    syncModal()
  }

  function scrollState(root) {
    if (!root) return []
    return [...root.querySelectorAll(scrollSelector)].map((node, index) => ({
      key: node.dataset.uiScrollKey || node.id || `${node.className}:${index}`,
      left: node.scrollLeft, top: node.scrollTop,
    }))
  }

  function restoreScroll(root, snapshot) {
    if (!root || !snapshot?.length) return
    const positions = new Map(snapshot.map(item => [item.key, item]))
    ;[...root.querySelectorAll(scrollSelector)].forEach((node, index) => {
      const saved = positions.get(node.dataset.uiScrollKey || node.id || `${node.className}:${index}`)
      if (saved && (node.scrollLeft !== saved.left || node.scrollTop !== saved.top)) {
        node.scrollLeft = saved.left; node.scrollTop = saved.top
        restoredScroll.set(node, { left: node.scrollLeft, top: node.scrollTop })
      }
    })
  }

  function consumeRestoredScroll(node) {
    const saved = restoredScroll.get(node)
    restoredScroll.delete(node)
    return !!saved && node.scrollLeft === saved.left && node.scrollTop === saved.top
  }

  function setPageMenu(html) {
    const host = document.getElementById('ui-overlay-host')
    if (!host) return
    host.querySelectorAll('.monitor-row-menu').forEach(node => node.remove())
    if (html) host.insertAdjacentHTML('beforeend', html)
  }

  function rememberMenuAnchor(menu, trigger) {
    const rect = trigger.getBoundingClientRect()
    menu.dataset.anchorX = String(rect.x)
    menu.dataset.anchorY = String(rect.y)
  }

  function menuAnchorMoved(menu, trigger) {
    if (!menu || !trigger) return true
    const rect = trigger.getBoundingClientRect()
    return Math.abs(rect.x - Number(menu.dataset.anchorX)) > .5
      || Math.abs(rect.y - Number(menu.dataset.anchorY)) > .5
  }

  // Selection is a state change, not a reason to replace rows or their controls.
  function setRowSelected(row, selected) {
    row.classList.toggle('selected', selected)
    row.setAttribute('aria-selected', String(selected))
    const input = row.querySelector('input[type="checkbox"]')
    if (input) input.checked = selected
    const button = row.querySelector('.check')
    if (button) { button.classList.toggle('is-checked', selected); button.setAttribute('aria-pressed', String(selected)) }
  }

  function patchNode(current, next) {
    if (current.nodeType !== next.nodeType || current.nodeName !== next.nodeName) {
      current.replaceWith(next); return next
    }
    if (current.isEqualNode(next)) return current
    if (current.nodeType === 3) { current.nodeValue = next.nodeValue; return current }
    if (current.nodeType !== 1) return current
    for (const attr of [...current.attributes]) if (!next.hasAttribute(attr.name)) current.removeAttribute(attr.name)
    for (const attr of [...next.attributes]) if (current.getAttribute(attr.name) !== attr.value) current.setAttribute(attr.name, attr.value)
    const previous = [...current.childNodes], incoming = [...next.childNodes]
    incoming.forEach((child, index) => {
      if (previous[index]) patchNode(previous[index], child)
      else current.append(child)
    })
    previous.slice(incoming.length).forEach(child => child.remove())
    return current
  }

  // Queue rows keep their identity, scroll position and progress animation.
  // The bounded lists share this projection helper rather than copying it.
  const listMarkup = new WeakMap()
  function reconcileRows(container, html) {
    if (!container || listMarkup.get(container) === html) return
    const template = document.createElement('template'); template.innerHTML = html
    const remaining = new Set(container.children)
    const occurrences = new Map()
    const keyFor = (node, index) => {
      const base = node.dataset.uiKey || `row:${index}`, occurrence = occurrences.get(base) || 0
      occurrences.set(base, occurrence + 1)
      return `${base}\u0000${occurrence}`
    }
    const previous = new Map([...container.children].map((node, index) => [keyFor(node, index), node]))
    occurrences.clear()
    let cursor = container.firstElementChild
    ;[...template.content.children].forEach((next, index) => {
      const key = keyFor(next, index)
      const current = previous.get(key)
      const node = current ? patchNode(current, next) : next
      if (current === cursor) cursor = node
      if (current) remaining.delete(current)
      previous.delete(key)
      if (node !== cursor) container.insertBefore(node, cursor)
      cursor = node.nextElementSibling
    })
    remaining.forEach(node => node.remove())
    listMarkup.set(container, html)
  }

  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return
    const modal = activeModal()
    if (!modal) return
    // The visible top dialog owns Escape. Reuse its close control so busy and
    // cancellation guards stay in the feature, not in a second UI state machine.
    const close = modal.querySelector('[data-ui-dismiss], .image-overlay-close, .cv-close')
    if (close && !close.disabled) close.click()
    event.preventDefault()
    event.stopImmediatePropagation()
  })

  document.addEventListener('focusin', event => {
    if (!(event.target instanceof HTMLElement)) return
    const modal = activeModal()
    if (modal?.contains(event.target)) modalFocus.set(modal, event.target)
    else if (!modal) lastOutsideFocus = event.target
  })
  // Standalone async dialogs use the same background lock as page-owned ones.
  // Observe structure only: our inert/class writes must not retrigger the observer.
  new MutationObserver(syncModal).observe(document.body, { childList: true, subtree: true })
  window.AppUI = { activeModal, syncModal, mountPageOverlays, setPageMenu, scrollState, restoreScroll, consumeRestoredScroll, rememberMenuAnchor, menuAnchorMoved, setRowSelected, reconcileRows }
})()
