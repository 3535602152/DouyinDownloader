/* Optional real-browser regression: npm install --no-save playwright
 * npx playwright install chromium
 * node --test tests/sidebar_browser.cjs
 * Set SIDEBAR_SCREENSHOTS to save screenshots and frame-by-frame geometry.
 * No Python bridge, login, external traffic or real account is used.
 */
const assert = require('node:assert/strict')
const fs = require('node:fs')
const http = require('node:http')
const path = require('node:path')
const test = require('node:test')
const { chromium } = require('playwright')

const webRoot = path.resolve(__dirname, '../gui/web')
const output = process.env.SIDEBAR_SCREENSHOTS
const iconSelector = '.brand-mark, .nav .nav-icon, .sidebar-toggle .nav-icon'

async function recordToggle(page, times = [0]) {
  return page.evaluate(({ selector, times }) => new Promise(resolve => {
    const icons = [...document.querySelectorAll(selector)]
    const sidebar = document.querySelector('.sidebar')
    const start = performance.now(), frames = []
    const sample = boundary => frames.push({ time: performance.now() - start, boundary,
      width: sidebar.getBoundingClientRect().width,
      x: icons.map(icon => icon.getBoundingClientRect().x),
    })
    let action = 0
    function frame() {
      while (action < times.length && performance.now() - start >= times[action]) {
        sample('before-toggle')
        document.querySelector('.sidebar-toggle').click()
        sample('after-toggle')
        action++
      }
      sample('frame')
      const elapsed = performance.now() - start - times.at(-1)
      const moving = sidebar.getAnimations().some(animation => animation.playState === 'running')
      // Under a busy software renderer, wall time can advance before the last
      // presentation frame. Record through transition completion, with a cap.
      if (elapsed < 400 || (moving && elapsed < 5000)) requestAnimationFrame(frame)
      else resolve(frames)
    }
    frame()
  }), { selector: iconSelector, times })
}

function assertNoBoundaryJump(frames) {
  frames.forEach((frame, i) => {
    if (frame.boundary !== 'after-toggle') return
    frame.x.forEach((x, j) => assert.ok(Math.abs(x - frames[i - 1].x[j]) <= 0.1,
      `Icon ${j} jumped at click boundary: ${frames[i - 1].x[j]} -> ${x}`))
  })
}

test('sidebar theme, persistence, keyboard, responsive layout and animation geometry', async t => {
  const server = http.createServer((request, response) => {
    const file = path.resolve(webRoot, '.' + new URL(request.url, 'http://localhost').pathname)
    if (file !== webRoot && !file.startsWith(webRoot + path.sep)) { response.writeHead(403).end(); return }
    const target = file === webRoot ? path.join(file, 'index.html') : file
    try {
      const type = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript' }[path.extname(target)]
      const data = fs.readFileSync(target)
      response.writeHead(200, { 'Content-Type': type || 'application/octet-stream' })
      response.end(data)
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
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`http://127.0.0.1:${server.address().port}`)
  await page.locator('.nav [data-page="settings"]').click()
  await page.locator('.settings-group').first().waitFor()
  await page.waitForTimeout(300)
  assert.equal(await page.locator('.settings-group-title').first().textContent(), '账号')
  const group = page.locator('.sidebar-theme')
  const rect = await group.boundingBox()
  assert.equal(rect.width, 136)
  assert.equal(rect.height, 34)
  assert.ok(rect.y + rect.height < (await page.locator('.sidebar-toggle').boundingBox()).y)
  if (output) { fs.mkdirSync(output, { recursive: true }); await page.screenshot({ path: path.join(output, 'after-light.png') }) }

  // Selecting a theme must not rebuild the page or discard an unsaved setting.
  await page.locator('[data-bind="path"]').fill('Example download folder')
  await page.locator('[data-theme-value="dark"]').press('Enter')
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark')
  assert.equal(await page.locator('[data-theme-value="dark"]').getAttribute('aria-pressed'), 'true')
  assert.equal(await page.locator('[data-bind="path"]').inputValue(), 'Example download folder')
  assert.equal(await page.evaluate(() => localStorage.getItem('douyin-downloader-theme')), 'dark')
  await page.waitForTimeout(200)
  if (output) await page.screenshot({ path: path.join(output, 'after-dark.png') })

  const collapse = await recordToggle(page)
  assertNoBoundaryJump(collapse)
  collapse.slice(1).forEach((frame, i) => frame.x.forEach((x, j) =>
    assert.ok(x <= collapse[i].x[j] + 0.1, `Icon ${j} moved right while collapsing`)))
  assert.equal(collapse.at(-1).width, 64)
  assert.equal(await group.isVisible(), false)
  const toggle = page.locator('.sidebar-theme-toggle')
  assert.equal(await toggle.isVisible(), true)
  assert.equal(await toggle.getAttribute('aria-label'), '切换到浅色主题')
  await toggle.focus()
  await page.keyboard.press('Tab')
  await page.keyboard.press('Shift+Tab')
  assert.equal(await page.locator('#nav-tip').textContent(), '切换到浅色主题')
  await toggle.press('Space')
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'light')
  assert.equal(await page.locator('#nav-tip').textContent(), '切换到深色主题')
  await page.keyboard.press('Escape')
  assert.equal(await page.locator('#nav-tip').isVisible(), false)
  await page.waitForTimeout(200)
  if (output) await page.screenshot({ path: path.join(output, 'after-collapsed.png') })

  await page.reload()
  await toggle.waitFor()
  assert.equal(await page.locator('html').getAttribute('data-sidebar'), 'collapsed')
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'light')
  const expand = await recordToggle(page)
  assertNoBoundaryJump(expand)
  expand.slice(1).forEach((frame, i) => frame.x.forEach((x, j) =>
    assert.ok(x >= expand[i].x[j] - 0.1, `Icon ${j} moved left while expanding`)))
  const interrupted = await recordToggle(page, [0, 65, 120, 175])
  assertNoBoundaryJump(interrupted)
  interrupted.forEach(frame => frame.x.forEach((x, j) => assert.ok(
    x >= collapse.at(-1).x[j] - 0.1 && x <= collapse[0].x[j] + 0.1, `Icon ${j} overshot during rapid toggles`)))

  // Ctrl+B and forced narrow mode must never strand focus on a hidden theme control.
  await page.locator('[data-theme-value="light"]').focus()
  await page.keyboard.press('Control+b')
  assert.equal(await page.evaluate(() => document.activeElement.className), 'sidebar-theme-toggle')
  await page.keyboard.press('Control+b')
  assert.equal(await page.evaluate(() => document.activeElement.dataset.themeValue), 'light')
  for (const width of [1200, 1024, 901, 900, 640]) {
    await page.setViewportSize({ width, height: 720 })
    await page.waitForTimeout(350)
    assert.equal(await group.isVisible(), width > 900)
    assert.equal(await toggle.isVisible(), width <= 900)
    assert.equal(await page.locator('.sidebar-toggle').isVisible(), width > 900)
    const button = width <= 900 ? toggle : group
    const box = await button.boundingBox()
    assert.ok(box.x >= 0 && box.y >= 0 && box.y + box.height <= 720)
  }
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.waitForTimeout(350)
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.locator('.sidebar-toggle').click()
  assert.equal((await page.locator('.sidebar').boundingBox()).width, 64)
  await page.locator('.sidebar-toggle').click()
  assert.equal((await page.locator('.sidebar').boundingBox()).width, 232)

  // Bridge persistence failure rolls back without rebuilding controls or losing focus.
  await page.evaluate(() => {
    window.pywebview = { api: { set_theme: async () => false, set_titlebar_theme: async () => true } }
  })
  await page.locator('[data-theme-value="dark"]').press('Enter')
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'light')
  assert.equal(await page.locator('[data-theme-value="light"]').getAttribute('aria-pressed'), 'true')
  assert.equal(await page.evaluate(() => localStorage.getItem('douyin-downloader-theme')), 'light')
  assert.equal(await page.evaluate(() => document.activeElement.dataset.themeValue), 'dark')
  assert.deepEqual(errors, [])
  if (output) fs.writeFileSync(path.join(output, 'animation-geometry.json'), JSON.stringify({ collapse, expand, interrupted }, null, 2))
})
