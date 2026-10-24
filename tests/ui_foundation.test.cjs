const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')
const root = path.join(__dirname, '..')
const source = fs.readFileSync(path.join(root, 'gui/web/ui-foundation.js'), 'utf8')

function harness() {
  const document = { body: {}, addEventListener() {}, querySelectorAll: () => [] }
  const context = { document, window: {}, MutationObserver: class { observe() {} } }
  vm.createContext(context)
  vm.runInContext(source, context)
  return { ui: context.window.AppUI, document }
}

function element({ key, className = 'table-wrap', left = 0, top = 0 } = {}) {
  const classes = new Set(), attributes = {}
  return { dataset: key ? { uiScrollKey: key } : {}, className, id: '', scrollLeft: left, scrollTop: top,
    classList: { toggle(name, on) { if (on) classes.add(name); else classes.delete(name) }, contains: name => classes.has(name) },
    setAttribute(name, value) { attributes[name] = value }, getAttribute: name => attributes[name],
    querySelector: () => null }
}

test('scroll snapshots follow stable component keys and do not cross page identities', () => {
  const { ui } = harness()
  const old = element({ key: 'monitor-table', left: 175 }), next = element({ key: 'monitor-table' })
  const state = ui.scrollState({ querySelectorAll: () => [old] })
  ui.restoreScroll({ querySelectorAll: () => [next] }, state)
  assert.equal(next.scrollLeft, 175)
  assert.equal(ui.consumeRestoredScroll(next), true)
  assert.equal(ui.consumeRestoredScroll(next), false)
  const otherPage = element({ key: 'following-page-2' })
  ui.restoreScroll({ querySelectorAll: () => [otherPage] }, ui.scrollState({ querySelectorAll: () => [element({ key: 'following-page-1', top: 400 })] }))
  assert.equal(otherPage.scrollTop, 0)
})

test('a genuine user scroll is not mistaken for a saved-position restoration', () => {
  const { ui } = harness(), node = element({ key: 'table' })
  ui.restoreScroll({ querySelectorAll: () => [node] }, [{ key: 'table', left: 100, top: 0 }])
  node.scrollLeft = 120
  assert.equal(ui.consumeRestoredScroll(node), false)
})

test('row selection updates native and button checkboxes without replacing controls', () => {
  const { ui } = harness(), row = element(), button = element(), checkbox = { checked: false }
  row.querySelector = selector => selector === '.check' ? button : checkbox
  ui.setRowSelected(row, true)
  assert.equal(row.classList.contains('selected'), true)
  assert.equal(row.getAttribute('aria-selected'), 'true')
  assert.equal(checkbox.checked, true)
  assert.equal(button.getAttribute('aria-pressed'), 'true')
  ui.setRowSelected(row, false)
  assert.equal(row.classList.contains('selected'), false)
  assert.equal(checkbox.checked, false)
  assert.equal(button.getAttribute('aria-pressed'), 'false')
})

test('menus ignore stale scroll events but detect movement of their anchor', () => {
  const { ui } = harness(), menu = element()
  let position = { x: 500, y: 200 }
  const trigger = { getBoundingClientRect: () => position }
  ui.rememberMenuAnchor(menu, trigger)
  assert.equal(ui.menuAnchorMoved(menu, trigger), false)
  position = { x: 400, y: 200 }
  assert.equal(ui.menuAnchorMoved(menu, trigger), true)
  assert.equal(ui.menuAnchorMoved(menu, null), true)
})

test('image previews take focus above the player and hidden dialogs are ignored', () => {
  const { ui, document } = harness()
  const modal = id => ({ id, hidden: false, getClientRects: () => [1], classList: { contains: name => id === name } })
  const first = modal('first'), last = modal('last'), hidden = { ...modal('hidden'), hidden: true }
  document.querySelectorAll = () => [first, last, hidden]
  assert.equal(ui.activeModal(), last)
  const player = modal('streamer-modal')
  document.querySelectorAll = () => [player, first, last]
  assert.equal(ui.activeModal(), player)
  const image = modal('image-overlay')
  document.querySelectorAll = () => [player, image, last]
  assert.equal(ui.activeModal(), image)
  image.hidden = true
  assert.equal(ui.activeModal(), player)
})

test('foundations load before pages and keep surface clipping separate from overlays', () => {
  const html = fs.readFileSync(path.join(root, 'gui/web/index.html'), 'utf8')
  const base = fs.readFileSync(path.join(root, 'gui/web/ui-foundation.css'), 'utf8')
  const pages = fs.readFileSync(path.join(root, 'gui/web/style.css'), 'utf8')
  assert.ok(html.indexOf('ui-foundation.css') < html.indexOf('href="style.css'))
  assert.ok(html.indexOf('ui-foundation.js') < html.indexOf('src="app.js'))
  assert.match(base, /\.panel \{[^}]*overflow: hidden/)
  assert.doesNotMatch(pages, /\.monitor-panel \{[^}]*overflow: visible/)
  assert.match(base, /--layer-popover: 20/)
  assert.match(base, /--layer-modal: 100/)
  assert.match(pages, /\.footer-panel \{[^}]*z-index: var\(--layer-popover\)/)
})
