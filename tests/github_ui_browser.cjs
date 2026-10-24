/* Real Chromium regression; isolated fixtures and no external traffic.
 * node --test tests/github_ui_browser.cjs
 * CHROMIUM_EXECUTABLE selects an existing Chromium/Edge installation.
 * GITHUB_UI_SCREENSHOTS optionally saves the layout review matrix. */
const assert = require('node:assert/strict')
const fs = require('node:fs')
const http = require('node:http')
const path = require('node:path')
const test = require('node:test')
const { chromium } = require('playwright')
const web = path.resolve(__dirname, '../gui/web')
const output = process.env.GITHUB_UI_SCREENSHOTS

async function preview(t, { nativeStartup = false } = {}) {
  const server = http.createServer((request, response) => {
    const file = path.resolve(web, '.' + new URL(request.url, 'http://localhost').pathname)
    if (!file.startsWith(web + path.sep) && file !== web) return response.writeHead(403).end()
    try {
      const target = file === web ? path.join(web, 'index.html') : file
      response.setHeader('Content-Type', { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' }[path.extname(target)] || 'application/octet-stream')
      response.end(fs.readFileSync(target))
    } catch { response.writeHead(404).end() }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const browser = await chromium.launch(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {})
  t.after(async () => {
    await browser.close()
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort())
  if (nativeStartup) await page.addInitScript(() => {
    window.chrome ??= {}
    window.chrome.webview = {}
    window.__startup = { pending: {}, calls: {} }
    const pending = name => {
      window.__startup.calls[name] = (window.__startup.calls[name] || 0) + 1
      return new Promise(resolve => { window.__startup.pending[name] = resolve })
    }
    window.pywebview = { api: {
      state: () => pending('state'), monitors: () => pending('monitors'),
      history: () => pending('history'), subscriptions: () => pending('subscriptions'),
      full_scan_progress: async () => null, scan_status: async () => ({ text: '空闲' }),
      poll: async () => [], set_titlebar_theme: async () => true,
      get_window_frame: async () => ({ custom: false, maximized: false }),
    } }
  })
  await page.goto(`http://127.0.0.1:${server.address().port}`)
  // Wait for the real initial page render before installing fixture state.
  // Its startup timer must not replace controls during identity assertions.
  await page.locator('#content .panel').first().waitFor()
  if (nativeStartup) {
    t.after(() => assert.deepEqual(errors, [], 'no uncaught errors during native startup'))
    return page
  }
  await page.evaluate(() => {
    window.__saved = Object.fromEntries(SettingsUI.fields().map(field => [field.state || field.key, field.default]))
    window.__saves = []
    applySettingsState(window.__saved)
    window.pywebview = { api: {
      set_titlebar_theme: async () => true,
      save_settings: async values => {
        window.__saves.push({ ...values })
        for (const field of SettingsUI.fields()) window.__saved[field.state || field.key] = values[field.form]
        return { ...window.__saved }
      },
      work_details: async id => ({ aweme_id: id, status: 'done', files: [{ path: '预览目录/作品.mp4', kind: 'media', size: 123456 }] }),
    } }
    state.page = 'settings'; renderPage()
  })
  if (output) fs.mkdirSync(output, { recursive: true })
  t.after(() => assert.deepEqual(errors, [], 'no uncaught errors in the real browser'))
  return page
}

async function resolveStartupSettings(page) {
  await page.evaluate(() => window.__startup.pending.state({
    ...Object.fromEntries(SettingsUI.fields().map(field => [field.state || field.key, field.default])),
    cookie_configured: true, path: '启动测试目录', links: [], mode: 'post', increase_by_mode: { post: true },
  }))
}

test('desktop paints before settings resolve and stays usable while snapshots are slow', async t => {
  const page = await preview(t, { nativeStartup: true })
  assert.match(await page.locator('#content').textContent(), /正在读取本地设置/)
  assert.equal(await page.locator('.sidebar .nav button').count(), 6)
  // A bridge present before app.js and a later ready event must not duplicate boot.
  await page.evaluate(() => window.dispatchEvent(new Event('pywebviewready')))
  assert.equal(await page.evaluate(() => window.__startup.calls.state), 1)
  await resolveStartupSettings(page)
  await page.locator('[data-action="link-preview"]').waitFor()
  assert.match(await page.locator('#busy-indicator').textContent(), /正在加载本地数据/)
  await page.locator('[data-page="settings"]').click()
  assert.equal(await page.locator('[data-bind="path"]').inputValue(), '启动测试目录')
  // No database snapshot has resolved; navigation and settings already work.
  assert.equal(await page.evaluate(() => state.startup), 'data')
  await page.evaluate(async () => {
    applyEvent({ type: 'monitors', rows: [{ sec_uid: 'newer', nickname: '较新的主播数据', enabled: true }] })
    applyEvent({ type: 'subscription_rows', rows: [{ id: 7 }] })
    window.pywebview.api.history = async () => ({ items: [], total: 75, page: 2, size: 50 })
    state.history.page = 2
    await refreshHistory()
    window.__startup.pending.monitors([])
    window.__startup.pending.subscriptions([])
  })
  await page.waitForTimeout(250)
  assert.equal(await page.evaluate(() => state.startup), 'data', 'slow history cannot block controls')
  await page.evaluate(() => window.__startup.pending.history({ items: [], total: 0, page: 1, size: 50 }))
  await page.waitForFunction(() => state.startup === '')
  assert.equal(await page.locator('.settings-group').count(), 6)
  assert.deepEqual(await page.evaluate(() => ({ monitors: state.monitors.map(row => row.sec_uid),
    subscriptions: state.subscriptions.map(row => row.id), historyPage: state.history.page })),
  { monitors: ['newer'], subscriptions: [7], historyPage: 2 }, 'late startup snapshots cannot overwrite newer data')
})

test('failed startup settings show retry and recover without empty content', async t => {
  const page = await preview(t, { nativeStartup: true })
  await page.evaluate(() => window.__startup.pending.state(null))
  await page.locator('[data-action="startup-retry"]').waitFor()
  assert.match(await page.locator('#content').textContent(), /本地设置读取失败/)
  await page.locator('[data-action="startup-retry"]').click()
  assert.match(await page.locator('#content').textContent(), /正在读取本地设置/)
  assert.equal(await page.evaluate(() => window.__startup.calls.state), 2)
  await resolveStartupSettings(page)
  await page.locator('[data-action="link-preview"]').waitFor()
  await page.evaluate(() => {
    window.__startup.pending.monitors([])
    window.__startup.pending.history({ items: [], total: 0, page: 1, size: 50 })
    window.__startup.pending.subscriptions([])
  })
  await page.waitForFunction(() => state.startup === '')
  assert.ok(await page.locator('#content').textContent())
})

test('six settings categories, shared search, validation, zero retry and responsive themes', async t => {
  const page = await preview(t)
  assert.equal(await page.locator('.settings-group').count(), 6)
  await page.locator('[data-bind="settingsSearch"]').fill('风控')
  assert.ok(await page.locator('[data-setting="scanStrategy"]').count())
  assert.equal(await page.locator('[data-bind="settingsSearch"]').evaluate(node => document.activeElement === node), true)
  await page.locator('[data-bind="settingsSearch"]').fill('')
  await page.locator('[data-bind="retry"]').fill('99')
  await page.locator('[data-bind="retry"]').press('Tab')
  await page.waitForTimeout(320)
  assert.equal(await page.evaluate(() => window.__saves.length), 0)
  assert.equal(await page.locator('[data-bind="retry"]').getAttribute('aria-invalid'), 'true')
  assert.match(await page.locator('#settings-save-status').textContent(), /未保存/)
  assert.equal(await page.locator('[data-action="settings-retry"]').isVisible(), true)
  await page.locator('[data-bind="retry"]').fill('0')
  await page.locator('[data-bind="retry"]').press('Tab')
  await page.waitForTimeout(320)
  assert.equal(await page.evaluate(() => window.__saved.retry), '0')
  await page.locator('[data-bind="quality"]').selectOption('720p')
  await page.waitForTimeout(320)
  assert.equal(await page.evaluate(() => state.forms.retry), '0')
  await page.locator('[data-action="toggle-cookie"]').click()
  await page.locator('[data-bind="cookie"]').fill('sessionid=preview-only')
  await page.locator('[data-action="setting-switch"][data-key="livePhotoMerge"]').click()
  await page.waitForTimeout(320)
  assert.equal(await page.locator('[data-bind="cookie"]').inputValue(), 'sessionid=preview-only')
  assert.equal(await page.evaluate(() => window.__saves.at(-1).cookie), '')
  await page.locator('[data-action="toggle-cookie"]').click()
  const geometry = []
  for (const theme of ['light', 'dark']) for (const width of [1440, 900, 640, 420]) {
    await page.setViewportSize({ width, height: 900 })
    await page.evaluate(theme => { state.forms.theme = theme; applyUiPreferences(); window.scrollTo(0, 0) }, theme)
    await page.waitForTimeout(280)
    const result = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth,
      categories: document.querySelectorAll('.settings-nav button').length }))
    assert.ok(result.scroll <= width + 1, `settings never overflow horizontally: ${JSON.stringify(result)}`)
    geometry.push({ theme, ...result })
    if (output && [1440, 640].includes(width)) await page.screenshot({ path: path.join(output, `settings-${theme}-${width}.png`), fullPage: true })
    if (output && theme === 'light' && width === 1440) await page.screenshot({ path: path.join(output, 'settings-overview-light.png') })
  }
  if (output) fs.writeFileSync(path.join(output, 'settings-layout-matrix.json'), JSON.stringify(geometry, null, 2))
})

test('system theme follows the OS and reduced motion controls shell and grid', async t => {
  const page = await preview(t)
  await page.evaluate(() => { state.forms.theme = 'system'; applyUiPreferences() })
  await page.emulateMedia({ colorScheme: 'dark' })
  await page.waitForFunction(() => document.documentElement.dataset.theme === 'dark')
  assert.equal(await page.locator('.sidebar-theme-toggle').getAttribute('aria-label'), '切换到浅色主题')
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark')
  await page.emulateMedia({ colorScheme: 'light' })
  await page.waitForFunction(() => document.documentElement.dataset.theme === 'light')
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'light')
  await page.evaluate(() => { state.forms.motion = 'reduced'; applyUiPreferences(); renderPage() })
  assert.equal(await page.locator('html').getAttribute('data-motion'), 'reduced')
  assert.equal(await page.evaluate(() => AppPreferences.motionAllowed()), false)
  assert.equal(await page.locator('#content').evaluate(node => getComputedStyle(node).animationName), 'none')
  await page.evaluate(() => { state.forms.motion = 'system'; applyUiPreferences() })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.waitForFunction(() => !AppPreferences.motionAllowed())
  assert.equal(await page.evaluate(() => AppPreferences.motionAllowed()), false)
  assert.equal(await page.evaluate(() => window.__saves.length), 0, 'OS changes do not save preferences')
})

test('edits made during an in-flight save survive its response and are saved next', async t => {
  const page = await preview(t)
  await page.evaluate(() => {
    const api = window.pywebview.api, save = api.save_settings
    window.__normalSave = save
    api.save_settings = values => new Promise(resolve => { window.__finishSave = async () => resolve(await save(values)) })
    state.forms.retry = 0
    window.__saving = saveSettings()
    state.forms.quality = '720p'
    state.forms.cookie = 'draft-not-submitted'
  })
  await page.evaluate(async () => { await window.__finishSave(); await window.__saving })
  assert.equal(await page.evaluate(() => state.forms.quality), '720p')
  assert.equal(await page.evaluate(() => state.forms.cookie), 'draft-not-submitted')
  await page.waitForTimeout(280)
  await page.evaluate(async () => { await window.__finishSave(); if (settingsSaving) await settingsSaving })
  await page.waitForTimeout(30)
  assert.equal(await page.evaluate(() => window.__saved.quality), '720p')
  assert.equal(await page.evaluate(() => window.__saves.length), 2)
  assert.equal(await page.evaluate(() => window.__saves.every(value => value.cookie === '')), true)
  await page.evaluate(async () => {
    window.pywebview.api.save_settings = async () => null
    state.forms.retry = 2; await saveSettings()
  })
  assert.equal(await page.locator('[data-action="settings-retry"]').isVisible(), true)
  assert.equal(await page.evaluate(() => state.forms.retry), 2, 'failed save keeps the draft')
  await page.evaluate(() => { window.pywebview.api.save_settings = window.__normalSave })
  await page.locator('[data-action="settings-retry"]').click()
  await page.waitForFunction(() => window.__saved.retry === 2)
  assert.equal(await page.locator('[data-action="settings-retry"]').isVisible(), false)
})

test('stable rows, a single responsive detail view, modal focus, and stale-response cleanup', async t => {
  const page = await preview(t)
  await page.evaluate(() => {
    state.page = 'link'; state.linkItems = [{ aweme_id: '123', title: '示例视频', status: '待下载', author_name: '示例作者' }]; renderPage()
    window.__row = document.querySelector('[data-work-id="123"]')
    window.__check = window.__row.querySelector('.check'); window.__check.focus()
    applyEvent({ type: 'row_status', scope: 'link', id: '123', status: '已存在' })
  })
  assert.equal(await page.evaluate(() => window.__row === document.querySelector('[data-work-id="123"]') && document.activeElement === window.__check), true)
  assert.match(await page.locator('[data-work-id="123"] .status-badge').textContent(), /已存在/)
  await page.locator('[data-action="work-details-open"]').click()
  await page.waitForTimeout(30)
  assert.equal(await page.locator('.work-detail-dialog').getAttribute('role'), 'region')
  assert.equal(await page.locator('#app').getAttribute('inert'), null)
  await page.evaluate(() => { window.__detail = document.querySelector('.work-detail-dialog') })
  if (output) await page.screenshot({ path: path.join(output, 'work-details-wide.png') })
  await page.setViewportSize({ width: 640, height: 900 })
  await page.waitForTimeout(280)
  assert.equal(await page.locator('.work-detail-dialog').getAttribute('role'), 'dialog')
  await page.evaluate(() => { document.documentElement.dataset.frame = 'custom' })
  assert.equal((await page.locator('.work-detail-dialog').boundingBox()).y, 40, 'drawer leaves the native window controls reachable')
  assert.equal(await page.evaluate(() => window.__detail === document.querySelector('.work-detail-dialog')), true)
  assert.equal(await page.locator('#app').getAttribute('inert'), '')
  assert.equal(await page.evaluate(() => document.querySelector('.work-detail-dialog').contains(document.activeElement)), true)
  if (output) await page.screenshot({ path: path.join(output, 'work-details-narrow.png') })
  await page.keyboard.press('Escape')
  assert.equal(await page.locator('#work-detail-host').isVisible(), false)
  assert.equal(await page.locator('#app').getAttribute('inert'), null)
  assert.equal(await page.locator('[data-action="work-details-open"]').evaluate(node => document.activeElement === node), true)
  await page.evaluate(() => { window.pywebview.api.work_details = () => new Promise(resolve => { window.__lateDetail = resolve }) })
  await page.locator('[data-action="work-details-open"]').click()
  await page.keyboard.press('Escape')
  await page.evaluate(() => window.__lateDetail({ status: 'done', files: [] }))
  await page.waitForTimeout(30)
  assert.equal(await page.locator('#work-detail-host').isVisible(), false)
})

test('queue patches preserve progress nodes, reorder safely and handle empty lists', async t => {
  const page = await preview(t)
  const result = await page.evaluate(() => {
    const list = document.createElement('div'); document.body.append(list)
    const row = (id, percent) => `<div class="dl-row" data-ui-key="${id}"><span>${id}</span><div class="dl-bar"><i style="width:${percent}%"></i></div></div>`
    AppUI.reconcileRows(list, row('a', 10) + row('b', 20))
    const first = list.children[0], progress = first.querySelector('i')
    AppUI.reconcileRows(list, row('b', 50) + row('a', 40))
    const stable = list.children[1] === first && first.querySelector('i') === progress && progress.style.width === '40%'
    AppUI.reconcileRows(list, '<div class="dl-empty">队列为空</div>')
    AppUI.reconcileRows(list, row('a', 100))
    const clean = list.children.length === 1 && list.firstElementChild.dataset.uiKey === 'a'
    AppUI.reconcileRows(list, row('same', 10) + row('same', 20))
    const duplicates = [...list.children]
    AppUI.reconcileRows(list, row('same', 40) + row('same', 50))
    const duplicateStable = duplicates.every((node, index) => node === list.children[index])
    list.remove(); return { stable, clean, duplicateStable }
  })
  assert.deepEqual(result, { stable: true, clean: true, duplicateStable: true })
})

test('batched preview preserves every item and final status with one page replacement', async t => {
  const page = await preview(t)
  const result = await page.evaluate(async () => {
    state.page = 'link'; state.linkItems = []; renderPage()
    const root = document.getElementById('content'); let replacements = 0
    const observer = new MutationObserver(records => { replacements += records.filter(record => record.target === root).length })
    observer.observe(root, { childList: true })
    const events = Array.from({ length: 100 }, (_, i) => ({ type: 'preview_item', scope: 'link', item: { aweme_id: String(i), title: `示例 ${i}`, status: '待下载' } }))
    events.push({ type: 'row_status', scope: 'link', id: '99', status: '已存在' })
    applyEventBatch(events)
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
    observer.disconnect()
    return { replacements, count: root.querySelectorAll('[data-work-id]').length,
      last: root.querySelector('[data-work-id="99"] .status-badge').textContent }
  })
  assert.deepEqual(result, { replacements: 1, count: 100, last: '已存在' })
})

test('media autoplay and gallery rotation obey preferences while manual playback remains available', async t => {
  const page = await preview(t)
  const result = await page.evaluate(async () => {
    AppPreferences.apply({ mediaAutoplay: false, galleryAutoplay: false })
    state.page = 'streamer'
    Object.assign(state.streamer, { secUid: 'preview', creator: { nickname: '预览作者' }, hasMore: false,
      works: [{ aweme_id: '1', title: '示例视频', media_type: 'video', media_detail_loaded: true,
        media_sources: ['/fixture.mp4'], cover_source: '', download_status: 'done' }] })
    renderPage(); await openStreamerWork(0)
    const video = document.getElementById('streamer-media-video')
    const videoPaused = !video.autoplay && video.paused
    state.streamer.modal = null
    state.streamer.works = [{ aweme_id: '2', title: '示例图文', media_type: 'image', media_detail_loaded: true,
      media_sources: ['/fixture-1.png', '/fixture-2.png'], live_sources: ['/fixture.mp4', ''], music_sources: [], download_status: 'done' }]
    await openStreamerWork(0)
    const modal = state.streamer.modal
    advanceStreamerGallery(100)
    const initialPaused = modal.galleryPaused && !modal.mediaStarted && modal.mediaIndex === 0
    toggleStreamerGallery()
    const manual = !modal.galleryPaused && modal.mediaStarted
    return { videoPaused, initialPaused, manual }
  })
  assert.deepEqual(result, { videoPaused: true, initialPaused: true, manual: true })
})
