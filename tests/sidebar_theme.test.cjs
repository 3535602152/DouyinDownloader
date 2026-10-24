const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')

const root = path.join(__dirname, '..')
const app = fs.readFileSync(path.join(root, 'gui/web/app.js'), 'utf8')
const html = fs.readFileSync(path.join(root, 'gui/web/index.html'), 'utf8')
const css = fs.readFileSync(path.join(root, 'gui/web/style.css'), 'utf8')
const source = app.slice(app.indexOf('const THEME_STORAGE_KEY'), app.indexOf('window.syncEffectiveTheme'))

function harness({ theme = 'light', saved = true, connected = true } = {}) {
  const element = (dataset = {}) => ({ dataset, attrs: {},
    setAttribute(key, value) { this.attrs[key] = value },
    hasAttribute(key) { return key in this.attrs },
    toggleAttribute(key, on) { if (on) this.attrs[key] = ''; else delete this.attrs[key] },
  })
  const light = element({ themeValue: 'light' }), dark = element({ themeValue: 'dark' })
  const group = element(), toggle = element(), documentRoot = element()
  const storage = new Map([['douyin-downloader-theme', theme]])
  const calls = [], titlebar = [], toasts = [], tips = []
  const api = { set_titlebar_theme(value) { titlebar.push(value); return Promise.resolve() } }
  const context = {
    state: { theme, themeSaving: false, settings: { theme } },
    window: { pywebview: connected ? { api } : null },
    document: { documentElement: documentRoot,
      querySelectorAll: () => [light, dark],
      querySelector: selector => selector === '.sidebar-theme' ? group : toggle,
    },
    localStorage: { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    call: async (name, value) => { calls.push([name, value]); return typeof saved === 'function' ? saved() : saved },
    bridge: () => connected ? api : null,
    showToast: text => toasts.push(text), showNavTip: target => tips.push(target.attrs['aria-label']),
  }
  vm.createContext(context)
  vm.runInContext(source, context)
  context.applyTheme(theme)
  return { context, light, dark, group, toggle, documentRoot, storage, calls, titlebar, toasts, tips }
}

test('light/dark selection stays synchronized with the root, titlebar and cache', async () => {
  const h = harness()
  assert.equal(h.light.attrs['aria-pressed'], 'true')
  await h.context.changeTheme('dark')
  assert.equal(h.context.state.theme, 'dark')
  assert.equal(h.documentRoot.dataset.theme, 'dark')
  assert.equal(h.documentRoot.hasAttribute('dark'), true)
  assert.equal(h.dark.attrs['aria-pressed'], 'true')
  assert.equal(h.light.attrs['aria-pressed'], 'false')
  assert.equal(h.toggle.attrs['aria-label'], '切换到浅色主题')
  assert.equal(h.storage.get('douyin-downloader-theme'), 'dark')
  assert.equal(h.context.state.settings.theme, 'dark')
  assert.equal(h.titlebar.at(-1), 'dark')
  await h.context.changeTheme('light')
  assert.equal(h.documentRoot.hasAttribute('dark'), false)
  assert.equal(h.toggle.attrs['aria-label'], '切换到深色主题')
  assert.deepEqual(h.calls, [['set_theme', 'dark'], ['set_theme', 'light']])
})

test('clicking the selected segment or an invalid theme does not save', async () => {
  const h = harness()
  await h.context.changeTheme('light')
  await h.context.changeTheme('invalid')
  assert.equal(h.calls.length, 0)
})

test('repeated clicks while saving are bounded and retain focusable controls', async () => {
  let resolve
  const h = harness({ saved: () => new Promise(done => { resolve = done }) })
  const pending = h.context.changeTheme('dark')
  assert.equal(h.group.attrs['aria-busy'], 'true')
  assert.equal(h.light.attrs['aria-disabled'], 'true')
  assert.equal(h.toggle.attrs['aria-disabled'], 'true')
  assert.equal(h.light.disabled, undefined)
  await h.context.changeTheme('light')
  await h.context.changeTheme('dark')
  assert.equal(h.calls.length, 1)
  resolve(true)
  await pending
  assert.equal(h.group.attrs['aria-busy'], 'false')
  assert.equal(h.light.attrs['aria-disabled'], 'false')
  assert.equal(h.context.state.theme, 'dark')
})

for (const saved of [false, null]) {
  test(`failed bridge save (${saved}) rolls back every theme surface`, async () => {
    const h = harness({ theme: 'dark', saved })
    await h.context.changeTheme('light')
    assert.equal(h.context.state.theme, 'dark')
    assert.equal(h.documentRoot.dataset.theme, 'dark')
    assert.equal(h.dark.attrs['aria-pressed'], 'true')
    assert.equal(h.toggle.attrs['aria-label'], '切换到浅色主题')
    assert.equal(h.storage.get('douyin-downloader-theme'), 'dark')
    assert.equal(h.context.state.settings.theme, 'dark')
    assert.equal(h.titlebar.at(-1), 'dark')
    assert.deepEqual(h.toasts, ['主题未能保存'])
  })
}

test('browser preview persists without a Python bridge and refreshes an open tooltip', async () => {
  const h = harness({ connected: false, saved: null })
  h.toggle.setAttribute('aria-describedby', 'nav-tip')
  await h.context.changeTheme('dark')
  assert.equal(h.storage.get('douyin-downloader-theme'), 'dark')
  assert.equal(h.toasts.length, 0)
  assert.equal(h.tips.at(-1), '切换到浅色主题')
})

test('unavailable localStorage does not prevent applying and saving the theme', async () => {
  const h = harness()
  h.context.localStorage.getItem = h.context.localStorage.setItem = () => { throw Error('unavailable') }
  assert.equal(h.context.readThemeCache(), 'light')
  await h.context.changeTheme('dark')
  assert.equal(h.documentRoot.dataset.theme, 'dark')
  assert.equal(h.context.state.themeSaving, false)
})

test('compact control uses official decorative Lucide SVGs and replaces the appearance card', () => {
  assert.match(html, /sidebar-footer[\s\S]*sidebar-theme[\s\S]*sidebar-toggle/)
  assert.match(html, /role="group" aria-label="颜色主题"/)
  assert.match(html, /M20\.985 12\.486a9 9/)
  assert.match(html, /<circle cx="12" cy="12" r="4"/)
  assert.match(html, /Lucide Icons and Contributors/)
  assert.match(html, /Copyright \(c\) 2013-present Cole Bemis/)
  assert.doesNotMatch(app, /settingsGroup\('外观'/)
  assert.match(css, /\.sidebar-theme \{[^}]*width: 136px; height: 34px;/)
})

test('collapsed icon anchors never depend on the sidebar width during transition', () => {
  const rule = css.match(/:root\[data-sidebar="collapsed"\] :is\(\.nav button, \.sidebar-toggle\) \{([^}]+)\}/)[1]
  assert.doesNotMatch(rule, /%|justify-content/)
  assert.match(rule, /padding-inline: 12\.5px/)
  const positions = [0, 0.1, 0.25, 0.5, 0.75, 1].map(progress =>
    (14 + (10 - 14) * progress) + (12 + (12.5 - 12) * progress))
  assert.equal(positions[0], 26)
  assert.equal(positions.at(-1), 22.5)
  positions.slice(1).forEach((x, index) => assert.ok(x <= positions[index]))
})
