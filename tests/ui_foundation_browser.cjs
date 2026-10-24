/* Optional Chromium integration suite, with isolated mock data and no bridge.
 * node --test tests/ui_foundation_browser.cjs
 * CHROMIUM_EXECUTABLE / CHROMIUM_ARGS (JSON array) select an existing browser.
 * UI_SCREENSHOTS saves the reviewed matrix and selected screenshots.
 */
const assert = require('node:assert/strict')
const fs = require('node:fs')
const http = require('node:http')
const path = require('node:path')
const test = require('node:test')
const { chromium } = require('playwright')
const root = path.resolve(__dirname, '../gui/web')
const output = process.env.UI_SCREENSHOTS

async function preview(t, contextOptions = {}) {
  const server = http.createServer((request, response) => {
    const file = path.resolve(root, '.' + new URL(request.url, 'http://localhost').pathname)
    if (file !== root && !file.startsWith(root + path.sep)) { response.writeHead(403).end(); return }
    const target = file === root ? path.join(root, 'index.html') : file
    try {
      response.setHeader('Content-Type', { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript' }[path.extname(target)] || 'application/octet-stream')
      response.end(fs.readFileSync(target))
    } catch { response.writeHead(404).end() }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const browser = await chromium.launch({
    ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}),
    ...(process.env.CHROMIUM_ARGS ? { args: JSON.parse(process.env.CHROMIUM_ARGS) } : {}),
  })
  t.after(async () => {
    await browser.close()
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  })
  const page = await browser.newPage({ ...contextOptions, viewport: { width: 1440, height: 900 } })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/*', route => {
    if (new URL(route.request().url()).hostname === '127.0.0.1') return route.continue()
    return route.abort()
  })
  await page.goto(`http://127.0.0.1:${server.address().port}`)
  await page.locator('.nav [data-page="following"]').click()
  await page.evaluate(() => {
    state.monitors = Array.from({ length: 96 }, (_, index) => ({
      sec_uid: `mock_${index}`, nickname: index ? `示例主播 ${index}` : '很长的主播名称用于检查省略和列宽稳定性',
      enabled: true, last_check_note: '正常', work_count: 120 + index, local_work_count: 80 + index,
      comments_enabled: false, profile_aweme_count: 80 + index, last_24h_downloaded: index % 3,
      local_storage_bytes: (80 + index) * 1048576, last_check_at: Math.floor(Date.now() / 1000) - (index + 1) * 60,
    }))
    renderPage()
  })
  await page.waitForTimeout(300)
  if (output) fs.mkdirSync(output, { recursive: true })
  return { page, errors }
}

async function pinnedGeometry(page) {
  return page.evaluate(() => {
    const panel = document.querySelector('#content .monitor-panel')
    const table = panel.querySelector('table'), fixed = document.querySelector('.monitor-fixed-header')
    const rect = fixed.getBoundingClientRect(), style = getComputedStyle(fixed)
    const cells = Array.from(table.tHead.rows[0].cells).map((cell, i) => {
      const clone = fixed.querySelector('thead').rows[0].cells[i]
      const sourceRect = cell.getBoundingClientRect(), cloneRect = clone.getBoundingClientRect()
      return { display: getComputedStyle(cell).display, cloneDisplay: getComputedStyle(clone).display,
        xDelta: cloneRect.x - sourceRect.x, widthDelta: cloneRect.width - sourceRect.width,
        text: cell.textContent, cloneText: clone.textContent }
    })
    return { header: rect.toJSON(), panel: panel.getBoundingClientRect().toJSON(),
      table: table.getBoundingClientRect().toJSON(),
      radius: style.borderRadius, shadow: style.boxShadow,
      topEdgesCovered: [rect.left + 1.5, rect.right - 1.5].every(x =>
        !!document.elementFromPoint(x, 1)?.closest('.monitor-fixed-header')),
      cells }
  })
}

function assertPinnedSeam(geometry) {
  assert.ok(Math.abs(geometry.header.y) < .05, 'the header meets the viewport without exposing rows above it')
  assert.ok(Math.abs(geometry.header.left - geometry.panel.left) < .05, 'shared left panel edge')
  assert.ok(Math.abs(geometry.header.right - geometry.panel.right) < .05, 'shared right panel edge')
  assert.equal(geometry.radius, '0px', 'the scrolled header must not reveal rows at rounded corners')
  assert.equal(geometry.shadow, 'none', 'the header shares a divider with the body, not a floating shadow')
  assert.equal(geometry.topEdgesCovered, true, 'no body row peeks through either upper corner')
  for (const cell of geometry.cells) {
    assert.equal(cell.cloneDisplay, cell.display)
    assert.equal(cell.cloneText, cell.text)
    if (cell.display === 'none') continue
    assert.ok(Math.abs(cell.xDelta) < .25 && Math.abs(cell.widthDelta) < .25,
      `every column stays aligned: ${JSON.stringify(cell)}`)
  }
}

async function settledPinnedGeometry(page) {
  // ResizeObserver + scroll + requestAnimationFrame can span multiple frames
  // on hosted runners. Keep the same strict geometry assertions, but wait for
  // the observed layout to converge instead of assuming a 35ms frame budget.
  let failure
  for (let i = 0; i < 40; i++) {
    try {
      const geometry = await pinnedGeometry(page)
      assertPinnedSeam(geometry)
      return geometry
    } catch (error) { failure = error }
    await page.waitForTimeout(50)
  }
  throw failure
}

test('pinned table header forms one continuous surface through resizing, scrolling and DPI changes', async t => {
  const matrix = []
  // DPI + proportionally reduced CSS viewport approximates 100/125/150/200%
  // desktop scaling; real Windows WebView2 zoom remains a separate integration check.
  for (const scale of [1, 1.25, 1.5, 2]) {
    const { page, errors } = await preview(t, { deviceScaleFactor: scale })
    for (const theme of ['light', 'dark']) for (const collapsed of [false, true]) {
      for (const width of [Math.round(1440 / scale), 640, 900, 901]) {
        await page.setViewportSize({ width, height: 720 })
        await page.evaluate(({ theme, collapsed }) => {
          state.theme = theme; applyTheme(theme); sidebarCollapsed = collapsed; syncSidebar()
          // A fractional parent width catches integer clientWidth truncation.
          document.querySelector('#content .monitor-panel').style.width = 'calc(100% - .5px)'
          window.scrollTo(0, 530)
        }, { theme, collapsed })
        await page.waitForTimeout(260)
        for (const scroll of [0, 137, 99999]) {
          await page.locator('#content .table-wrap').evaluate((node, scroll) => { node.scrollLeft = scroll }, scroll)
          await page.waitForTimeout(35)
          const geometry = await settledPinnedGeometry(page)
          assertPinnedSeam(geometry)
          matrix.push({ scale, theme, collapsed, width, scroll, geometry })
        }
        if (output && collapsed && scale === 1 && [1440, 640].includes(width)) {
          await page.locator('#content .table-wrap').evaluate(node => { node.scrollLeft = 0 })
          await page.waitForTimeout(35)
          await page.screenshot({ path: path.join(output, `seam-${theme}-${width}.png`) })
        }
      }
    }
    assert.deepEqual(errors, [])
    await page.close()
  }
  if (output) fs.writeFileSync(path.join(output, 'header-seam-matrix.json'), JSON.stringify(matrix, null, 2))
})

test('pinned header tracks capped-content motion, column rebuilds and table entry/exit', async t => {
  const { page, errors } = await preview(t)
  await page.setViewportSize({ width: 1920, height: 900 })
  await page.evaluate(() => { window.scrollTo(0, 530) })
  await page.waitForTimeout(280)
  const contentWidth = await page.locator('#content').evaluate(node => node.getBoundingClientRect().width)
  for (const collapsed of [true, false, true]) {
    await page.evaluate(collapsed => { sidebarCollapsed = collapsed; syncSidebar() }, collapsed)
    await page.waitForTimeout(280)
    assert.equal(await page.locator('#content').evaluate(node => node.getBoundingClientRect().width), contentWidth)
    assertPinnedSeam(await settledPinnedGeometry(page))
  }
  // The source's configured columns remain authoritative across rebuilds.
  const originalColumns = await page.locator('#content .monitor-panel th').count()
  await page.evaluate(() => {
    state.hiddenMonitorColumns.add('monitor')
    state.monitorColumns = [...state.monitorColumns].reverse()
    renderPage()
  })
  await page.waitForTimeout(40)
  assert.equal(await page.locator('#content .monitor-panel th').count(), originalColumns - 1)
  assert.equal(await page.locator('.monitor-fixed-header th').count(), originalColumns - 1)
  assertPinnedSeam(await settledPinnedGeometry(page))
  await page.evaluate(() => {
    state.hiddenMonitorColumns.delete('monitor')
    state.monitorColumns = [...state.monitorColumns].reverse()
    renderPage()
  })
  await page.waitForTimeout(40)
  assert.equal(await page.locator('.monitor-fixed-header th').count(), originalColumns)
  assertPinnedSeam(await settledPinnedGeometry(page))
  // Crossing the viewport edge must neither pin early nor leave a top gap.
  const threshold = await page.locator('#content .monitor-panel thead').evaluate(node => node.getBoundingClientRect().top + scrollY)
  await page.evaluate(y => window.scrollTo(0, Math.floor(y) - 1), threshold)
  await page.waitForTimeout(40)
  assert.equal(await page.locator('.monitor-fixed-header').count(), 0)
  await page.evaluate(y => window.scrollTo(0, Math.ceil(y) + 1), threshold)
  await page.waitForTimeout(40)
  assertPinnedSeam(await settledPinnedGeometry(page))
  // Extra content makes the table's lower boundary reachable in the viewport.
  const bottom = await page.evaluate(() => {
    const spacer = document.createElement('div')
    spacer.id = 'test-bottom-spacer'; spacer.style.height = '1000px'
    document.querySelector('#content').appendChild(spacer)
    return document.querySelector('#content .monitor-panel table').getBoundingClientRect().bottom + scrollY
  })
  await page.evaluate(y => window.scrollTo(0, y - 20), bottom)
  await page.waitForTimeout(40)
  const ending = await pinnedGeometry(page)
  assert.ok(ending.header.top < 0, 'header is pushed off with the table')
  assert.ok(Math.abs(ending.header.bottom - ending.table.bottom) < .05, 'no trailing header beyond the body')
  await page.evaluate(y => window.scrollTo(0, y + 1), bottom)
  await page.waitForTimeout(40)
  assert.equal(await page.locator('.monitor-fixed-header').count(), 0)
  await page.evaluate(() => { window.scrollTo(0, 530) })
  await page.waitForTimeout(40)
  assertPinnedSeam(await settledPinnedGeometry(page))
  await page.evaluate(() => { state.monitors = []; renderPage() })
  assert.equal(await page.locator('.monitor-fixed-header').count(), 0)
  assert.deepEqual(errors, [])
})

test('surface/table matrix, local selection and progress retain scroll, nodes and focus', async t => {
  const { page, errors } = await preview(t)
  const matrix = []
  for (const theme of ['light', 'dark']) for (const collapsed of [false, true]) {
    for (const width of [1440, 1024, 901, 900, 640, 400]) {
      await page.setViewportSize({ width, height: 720 })
      await page.evaluate(({ theme, collapsed }) => {
        state.theme = theme; applyTheme(theme); sidebarCollapsed = collapsed; syncSidebar(); window.scrollTo(0, 0)
      }, { theme, collapsed })
      await page.waitForFunction(expected => Math.abs(document.querySelector('.sidebar').getBoundingClientRect().width - expected) < .01, collapsed || width <= 900 ? 64 : width <= 1200 ? 200 : 232)
      const result = await page.evaluate(() => {
        const panel = document.querySelector('.monitor-panel'), scroll = panel.querySelector('.table-wrap')
        const r = panel.getBoundingClientRect(), style = getComputedStyle(panel)
        return { width: innerWidth, theme: state.theme, collapsed: sidebarIsCollapsed(),
          documentWidth: document.documentElement.scrollWidth, panelOverflow: style.overflow,
          radius: style.borderTopLeftRadius, cornerIsCell: !!document.elementFromPoint(r.x + 1, r.y + 1)?.closest('th'),
          scrollWidth: scroll.scrollWidth, clientWidth: scroll.clientWidth,
          sidebarWidth: document.querySelector('.sidebar').getBoundingClientRect().width }
      })
      matrix.push(result)
      assert.equal(result.documentWidth, width, JSON.stringify(result))
      assert.equal(result.panelOverflow, 'hidden')
      assert.equal(result.cornerIsCell, false)
      assert.equal(result.radius, '8px')
      assert.equal(result.sidebarWidth, collapsed || width <= 900 ? 64 : width <= 1200 ? 200 : 232)
      if (output && (width === 1440 || (width === 640 && collapsed))) {
        await page.screenshot({ path: path.join(output, `table-${theme}-${collapsed ? 'collapsed' : 'expanded'}-${width}.png`) })
      }
    }
  }
  await page.setViewportSize({ width: 1024, height: 720 })
  await page.waitForTimeout(250)
  await page.evaluate(() => {
    const scroll = document.querySelector('.table-wrap')
    scroll.scrollLeft = 150
    window.savedTable = scroll
    window.savedFirstRow = scroll.querySelector('tbody tr')
  })
  await page.locator('.monitor-panel tbody tr').first().locator('td').nth(7).click()
  assert.deepEqual(await page.evaluate(() => ({
    scroll: savedTable.scrollLeft, sameTable: savedTable === document.querySelector('.table-wrap'),
    sameRow: savedFirstRow === document.querySelector('.monitor-panel tbody tr'),
    selected: savedFirstRow.getAttribute('aria-selected'),
    mixed: document.querySelector('[data-action="monitor-select-all"]').getAttribute('aria-pressed'),
  })), { scroll: 150, sameTable: true, sameRow: true, selected: 'true', mixed: 'mixed' })
  await page.locator('.table-wrap').evaluate(node => { node.scrollLeft = 0 })
  const checkbox = page.locator('.monitor-row-select').first()
  await checkbox.focus()
  await checkbox.press('Space')
  assert.equal(await checkbox.isChecked(), false)
  assert.equal(await checkbox.evaluate(node => document.activeElement === node), true)

  // A background snapshot may replace rows, but not their user's view position.
  await page.locator('.table-wrap').evaluate(node => { node.scrollLeft = 150 })
  await page.evaluate(() => { state.monitors[0].work_count++; renderPage() })
  assert.equal(await page.locator('.table-wrap').evaluate(node => node.scrollLeft), 150)
  assert.equal(await checkbox.evaluate(node => document.activeElement === node), true)
  await page.evaluate(() => {
    window.savedFirstRow = document.querySelector('.monitor-panel tbody tr')
    window.savedProgress = document.querySelector('[data-action="monitor-validate-local"]')
    applyEvent({ type: 'local_validate_progress', scanned: 4, total: 10 })
  })
  assert.deepEqual(await page.evaluate(() => ({ same: savedFirstRow === document.querySelector('.monitor-panel tbody tr'),
    control: savedProgress === document.querySelector('[data-action="monitor-validate-local"]'),
    progress: savedProgress.style.getPropertyValue('--progress'), busy: savedProgress.getAttribute('aria-busy'),
    scroll: document.querySelector('.table-wrap').scrollLeft })), { same: true, control: true, progress: '40%', busy: 'true', scroll: 150 })
  await page.evaluate(() => applyEvent({ type: 'local_validate_done', reset_count: 0 }))
  assert.equal(await page.locator('[data-action="monitor-validate-local"]').isEnabled(), true)
  assert.deepEqual(errors, [])
  if (output) fs.writeFileSync(path.join(output, 'layout-matrix.json'), JSON.stringify(matrix, null, 2))
})

test('portal menus and modals obey layers, keyboard, dismissal and focus return', async t => {
  const { page, errors } = await preview(t)
  for (const width of [1440, 640, 400]) {
    await page.setViewportSize({ width, height: 720 })
    await page.waitForTimeout(250)
    const trigger = page.locator('[data-action="monitor-menu-toggle"]').first()
    await trigger.click()
    assert.equal(await page.locator('#monitor-row-menu').count(), 1, `row menu at ${width}px; ${errors}`)
    const menu = page.locator('#monitor-row-menu')
    assert.equal(await menu.evaluate(node => node.parentElement.id), 'ui-overlay-host')
    await page.evaluate(() => renderPage())
    await page.waitForTimeout(30)
    assert.equal(await menu.count(), 1)
    assert.equal(await page.evaluate(() => document.activeElement.dataset.action), 'monitor-check-latest')
    const rect = await menu.boundingBox()
    assert.ok(rect.x >= 0 && rect.x + rect.width <= width && rect.y >= 0 && rect.y + rect.height <= 720)
    await page.keyboard.press('End')
    assert.equal(await page.evaluate(() => document.activeElement.dataset.action), 'monitor-delete-open')
    await page.keyboard.press('Home')
    assert.equal(await page.evaluate(() => document.activeElement.dataset.action), 'monitor-check-latest')
    await page.keyboard.press('Escape')
    assert.equal(await menu.count(), 0)
    assert.equal(await trigger.evaluate(node => document.activeElement === node), true)
    await trigger.click()
    await page.locator('#page-title').click()
    assert.equal(await menu.count(), 0)

    await page.locator('[data-action="monitor-add-open"]').click()
    const overlay = page.locator('[data-monitor-add-overlay]')
    assert.equal(await overlay.evaluate(node => node.parentElement.id), 'ui-overlay-host')
    assert.equal(await page.locator('#app').evaluate(node => node.inert), true)
    const box = await overlay.boundingBox()
    assert.equal(box.x, 0); assert.equal(box.y, 0); assert.equal(box.width, width); assert.equal(box.height, 720)
    await page.locator('[data-bind="monitorUrls"]').fill('example unsaved text')
    await page.evaluate(() => { renderPage(); ScanPanel.show() })
    assert.equal(await page.locator('[data-bind="monitorUrls"]').inputValue(), 'example unsaved text')
    assert.equal(await page.locator('#footer-panel').isVisible(), false)
    await page.keyboard.press('Shift+Tab')
    assert.equal(await page.evaluate(() => !!document.activeElement.closest('[data-monitor-add-overlay]')), true)
    await page.keyboard.press('Escape')
    assert.equal(await overlay.count(), 0)
    assert.equal(await page.locator('#app').evaluate(node => node.inert), false)
    assert.equal(await page.evaluate(() => document.activeElement.dataset.action), 'monitor-add-open')
  }
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.evaluate(() => { state.theme = 'dark'; applyTheme('dark'); window.scrollTo(0, 0); ScanPanel.show() })
  await page.waitForTimeout(300)
  assert.equal(await page.locator('#footer-panel').isVisible(), true)
  // Starting a modal also dismisses a popover that was already open.
  await page.evaluate(() => openMonitorDelete(['mock_0']))
  assert.equal(await page.locator('#footer-panel').isVisible(), false)
  assert.equal(await page.locator('[data-monitor-delete-overlay]').isVisible(), true)
  if (output) await page.screenshot({ path: path.join(output, 'modal-dark.png') })
  await page.locator('[data-monitor-delete-overlay]').click({ position: { x: 3, y: 3 } })
  assert.equal(await page.locator('[data-monitor-delete-overlay]').count(), 0)
  assert.equal(await page.locator('#app').evaluate(node => node.inert), false)
  await page.locator('[data-action="toggle-column-picker"]').click()
  assert.equal(await page.locator('[data-column-picker]').isVisible(), true)
  await page.keyboard.press('Escape')
  assert.equal(await page.locator('[data-column-picker]').count(), 0)
  assert.equal(await page.evaluate(() => document.activeElement.dataset.action), 'toggle-column-picker')
  assert.deepEqual(errors, [])
})

test('long import lists retain selection, scroll and controls; standalone dialogs release locks', async t => {
  const { page, errors } = await preview(t)
  await page.locator('[data-action="following-toggle"]').focus()
  await page.evaluate(() => {
    Object.assign(state.followingImport, { open: true, phase: 'ready', page: 1, selected: new Set(),
      items: Array.from({ length: 80 }, (_, i) => ({ sec_uid: `import_${i}`, nickname: `导入示例 ${i}`, can_import: true })) })
    renderPage()
  })
  await page.locator('.following-list-wrap').evaluate(node => { node.scrollTop = 500; window.savedImportScroll = node })
  const rowButton = page.locator('[data-action="following-toggle-row"]').nth(10)
  await rowButton.click()
  assert.equal(await rowButton.getAttribute('aria-pressed'), 'true')
  assert.equal(await page.evaluate(() => savedImportScroll === document.querySelector('.following-list-wrap')), true)
  const top = await page.locator('.following-list-wrap').evaluate(node => node.scrollTop)
  assert.ok(top > 0)
  await page.evaluate(() => renderPage())
  assert.equal(await page.locator('.following-list-wrap').evaluate(node => node.scrollTop), top)
  assert.equal(await rowButton.evaluate(node => document.activeElement === node), true)
  await page.locator('[data-action="following-page-next"]').click()
  assert.equal(await page.locator('.following-list-wrap').evaluate(node => node.scrollTop), 0)
  await page.keyboard.press('Escape')
  assert.equal(await page.locator('#app').evaluate(node => node.inert), false)
  assert.equal(await page.evaluate(() => document.activeElement.dataset.action), 'following-toggle')

  // An asynchronously arriving QR dialog must not let Escape dismiss the
  // underlying page dialog first.
  await page.locator('[data-action="monitor-add-open"]').click()
  await page.locator('[data-bind="monitorUrls"]').focus()
  await page.evaluate(() => { state.qrLogin = { mode: 'app', status: 'loading', image: '', error: '' }; renderQrLogin() })
  await page.waitForTimeout(30)
  await page.keyboard.press('Escape')
  assert.equal(await page.locator('[data-qr-login-overlay]').count(), 0)
  assert.equal(await page.locator('[data-monitor-add-overlay]').count(), 1)
  assert.equal(await page.evaluate(() => document.activeElement.dataset.bind), 'monitorUrls')
  assert.equal(await page.locator('#app').evaluate(node => node.inert), true)
  await page.keyboard.press('Escape')
  assert.equal(await page.locator('[data-monitor-add-overlay]').count(), 0)
  assert.equal(await page.locator('#app').evaluate(node => node.inert), false)

  await page.locator('.nav [data-page="settings"]').click()
  await page.locator('[data-action="qr-login"]').focus()
  await page.evaluate(() => { state.qrLogin = { mode: 'app', status: 'loading', image: '', error: '' }; renderQrLogin() })
  await page.waitForTimeout(30)
  assert.equal(await page.locator('#app').evaluate(node => node.inert), true)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(30)
  assert.equal(await page.locator('#app').evaluate(node => node.inert), false)
  assert.equal(await page.evaluate(() => document.activeElement.dataset.action), 'qr-login')
  assert.deepEqual(errors, [])
})

test('page navigation and short-window layout preserve shared controls and external focus', async t => {
  const { page, errors } = await preview(t)
  for (const theme of ['light', 'dark']) {
    await page.evaluate(theme => { state.theme = theme; applyTheme(theme) }, theme)
    for (const name of ['link', 'user', 'comments', 'history', 'settings', 'following']) {
      await page.locator(`.nav [data-page="${name}"]`).click()
      await page.waitForTimeout(250)
      assert.equal(await page.locator('.nav [aria-current="page"]').getAttribute('data-page'), name)
      assert.equal(await page.locator('#app').evaluate(node => node.inert), false)
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 1440)
      if (output && ['settings', 'user'].includes(name)) {
        await page.screenshot({ path: path.join(output, `${name}-${theme}.png`) })
      }
    }
  }
  // A data refresh must not steal keyboard focus from persistent app chrome.
  await page.locator('.nav [data-page="following"]').focus()
  await page.evaluate(() => renderPage())
  assert.equal(await page.evaluate(() => document.activeElement.dataset.page), 'following')
  await page.setViewportSize({ width: 640, height: 480 })
  await page.locator('[data-action="monitor-add-open"]').click()
  const bounds = await page.locator('.monitor-add-dialog').boundingBox()
  assert.ok(bounds.x >= 0 && bounds.y >= 0 && bounds.x + bounds.width <= 640 && bounds.y + bounds.height <= 480)
  await page.keyboard.press('Escape')
  assert.equal(await page.locator('#app').evaluate(node => node.inert), false)
  assert.deepEqual(errors, [])
})

test('latest main storage formatting and pinned header remain synchronized and modal-safe', async t => {
  const { page, errors } = await preview(t)
  await page.setViewportSize({ width: 1024, height: 720 })
  await page.evaluate(() => {
    state.theme = 'dark'; applyTheme('dark'); sidebarCollapsed = true; syncSidebar()
    window.scrollTo(0, 260)
  })
  await page.locator('.monitor-fixed-header').waitFor()
  await page.waitForFunction(() => document.querySelector('.sidebar').getBoundingClientRect().width === 64)
  assert.equal(await page.evaluate(() => formatMonitorStorageBytes(1577 * 1024 * 1024)), '1,577 MB')
  await page.locator('#content .table-wrap').evaluate(node => { node.scrollLeft = 150 })
  await page.waitForFunction(() => Math.abs(document.querySelector('.monitor-fixed-header th').getBoundingClientRect().x - document.querySelector('#content .table-wrap th').getBoundingClientRect().x) < .5)
  assert.ok(Math.abs(await page.evaluate(() => document.querySelector('.monitor-fixed-header th').getBoundingClientRect().x
    - document.querySelector('#content .table-wrap th').getBoundingClientRect().x)) < .5)
  assert.equal(Math.round((await page.locator('.monitor-fixed-header').boundingBox()).y), 0)
  await page.evaluate(() => { sidebarCollapsed = false; syncSidebar() })
  await page.waitForFunction(() => Math.abs(document.querySelector('.monitor-fixed-header').getBoundingClientRect().x - document.querySelector('#content .monitor-panel').getBoundingClientRect().x) < .1
    && document.querySelector('.sidebar').getBoundingClientRect().width === 200)
  await page.locator('#content .table-wrap').evaluate(node => { node.scrollLeft = 0 })
  await page.waitForTimeout(80)
  const selectAll = page.locator('.monitor-fixed-header [data-action="monitor-select-all"]')
  await selectAll.click()
  assert.equal(await page.locator('#content [data-action="monitor-select-all"]').getAttribute('aria-pressed'), 'true')
  assert.equal(await selectAll.getAttribute('aria-pressed'), 'true')
  await page.evaluate(() => renderPage())
  assert.equal(await page.evaluate(() => document.activeElement.dataset.uiFocusKey), 'fixed-monitor-select-all')
  assert.equal(await selectAll.getAttribute('aria-pressed'), 'true')
  await page.evaluate(() => openMonitorDelete(['mock_0']))
  assert.equal(await page.locator('.monitor-fixed-header').evaluate(node => node.inert), true)
  await page.keyboard.press('Escape')
  await page.waitForTimeout(30)
  assert.equal(await page.locator('.monitor-fixed-header').evaluate(node => node.inert), false)
  if (output) await page.screenshot({ path: path.join(output, 'pinned-header-dark.png') })
  await page.locator('.nav [data-page="settings"]').click()
  assert.equal(await page.locator('.monitor-fixed-header').count(), 0)
  assert.deepEqual(errors, [])
})
