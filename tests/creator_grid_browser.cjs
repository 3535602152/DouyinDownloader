/* Isolated Chromium regression for creator auto-fill reflow. No bridge/account.
 * node --test tests/creator_grid_browser.cjs
 * CHROMIUM_EXECUTABLE / CHROMIUM_ARGS select an existing browser.
 * GRID_SCREENSHOTS saves visual / frame geometry evidence outside the repo.
 */
const assert = require('node:assert/strict')
const fs = require('node:fs')
const http = require('node:http')
const path = require('node:path')
const test = require('node:test')
const { chromium } = require('playwright')
const root = path.resolve(__dirname, '../gui/web')
const output = process.env.GRID_SCREENSHOTS

async function preview(t, options = {}) {
  const server = http.createServer((request, response) => {
    const file = path.resolve(root, '.' + new URL(request.url, 'http://localhost').pathname)
    if (file !== root && !file.startsWith(root + path.sep)) { response.writeHead(403).end(); return }
    try {
      const target = file === root ? path.join(root, 'index.html') : file
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
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, ...options })
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/*', route => {
    if (new URL(route.request().url()).hostname === '127.0.0.1') return route.continue()
    return route.abort()
  })
  await page.goto(`http://127.0.0.1:${server.address().port}`)
  await page.evaluate(() => {
    const works = Array.from({ length: 72 }, (_, index) => ({
      aweme_id: `mock_${index}`, title: `作品 ${index + 1} · 测试封面与连续换行`, media_type: 'video',
      cover_source: 'data:image/svg+xml,' + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="300" height="400"><rect width="300" height="400" fill="hsl(${index * 37 % 360} 36% 28%)"/><circle cx="150" cy="160" r="72" fill="hsl(${index * 37 % 360} 50% 65%)"/><text x="150" y="300" text-anchor="middle" font-size="50" fill="white">${String(index + 1).padStart(2, '0')}</text></svg>`),
      create_time: 1700000000 - index * 86400, like_count: 2345 + index,
    }))
    state.page = 'streamer'
    Object.assign(state.streamer, { secUid: 'mock', creator: { nickname: '动效演示', signature: '封面等比例缩放，卡片连续移动', aweme_count: works.length },
      works, profileKnown: true, loading: false, hasMore: false, source: 'local', subtab: 'all', error: '', search: '', date: '', modal: null })
    renderPage()
    window.scrollTo(0, document.querySelector('#cv-work-list').getBoundingClientRect().top + scrollY - 72)
  })
  await page.waitForTimeout(350)
  if (output) fs.mkdirSync(output, { recursive: true })
  return { page, errors }
}

async function geometry(page) {
  return page.evaluate(() => [...document.querySelectorAll('#cv-work-list > .cv-card')].map(card => {
    const rect = card.getBoundingClientRect(), cover = card.querySelector('.cv-card-thumb').getBoundingClientRect()
    return { x: rect.x, y: rect.y + scrollY, width: rect.width, height: rect.height,
      ratio: cover.width / cover.height, transform: getComputedStyle(card).transform,
      moving: card.getAnimations().length }
  }))
}

async function record(page, times = [0]) {
  return page.evaluate(times => new Promise(resolve => {
    const cards = [...document.querySelectorAll('#cv-work-list > .cv-card')]
    const start = performance.now(), frames = []
    let action = 0
    const grid = document.querySelector('#cv-work-list')
    let width = grid.getBoundingClientRect().width
    const capture = () => frames.push({ time: performance.now() - start, scroll: scrollY, cards: cards.map(card => {
      const rect = card.getBoundingClientRect(), cover = card.querySelector('.cv-card-thumb').getBoundingClientRect()
      return { x: rect.x, y: rect.y + scrollY, width: rect.width, ratio: cover.width / cover.height,
        transform: getComputedStyle(card).transform, animations: card.getAnimations().length }
    }) })
    // Registered after the controller, this observer samples the final
    // pre-paint result, not the transient layout before FLIP's inversion.
    const observer = new ResizeObserver(() => { width = grid.getBoundingClientRect().width; capture() })
    observer.observe(grid)
    function sample() {
      const now = performance.now() - start
      while (action < times.length && now >= times[action]) { toggleSidebar(); action++ }
      if (Math.abs(grid.getBoundingClientRect().width - width) < .1) capture()
      if (performance.now() - start < times.at(-1) + 500) requestAnimationFrame(sample)
      else { observer.disconnect(); resolve(frames) }
    }
    requestAnimationFrame(sample)
  }), times)
}

test('visible covers resize without cross-row flight; reversals preserve nodes, scroll and focus', async t => {
  const { page, errors } = await preview(t)
  const matrix = []
  for (const width of [1440, 1200, 1024, 901]) {
    await page.setViewportSize({ width, height: 900 })
    await page.evaluate(() => { sidebarCollapsed = true; syncSidebar() })
    await page.waitForTimeout(400)
    const before = await geometry(page)
    await page.locator('.cv-card-link').first().focus()
    const identity = await page.locator('.cv-card-link').first().getAttribute('data-aweme-id')
    const frames = await record(page)
    const after = await geometry(page)
    const wrapped = before.findIndex((card, index) => index > 0 && card.y === before[0].y && after[index].y > after[0].y + 80)
    assert.ok(wrapped > 0, `a last first-row item wraps at ${width}`)
    for (const frame of frames) for (const card of frame.cards) {
      if (card.transform === 'none') continue
      const values = card.transform.slice(7, -1).split(',').map(Number)
      assert.equal(values[4], 0, 'no horizontal positional flight')
      assert.equal(values[5], 0, 'no cross-row positional flight')
    }
    assert.ok(frames.some(frame => frame.cards[wrapped].animations > 0), 'native transform animation runs')
    for (const frame of frames) for (const card of frame.cards.slice(0, 12)) assert.ok(Math.abs(card.ratio - .75) < .001, 'covers keep their 3:4 ratio')
    assert.ok(after.every(card => card.transform === 'none' && card.moving === 0), 'transforms finish cleanly')
    assert.equal(await page.evaluate(() => document.activeElement.dataset.awemeId), identity, 'focused work stays focused')
    assert.ok(frames.every(frame => Math.abs(frame.scroll - frames[0].scroll) < 1), 'reflow does not scroll the document')
    const reverse = await record(page)
    const restored = await geometry(page)
    assert.ok(restored.every((card, index) => Math.abs(card.x - before[index].x) < .1 && Math.abs(card.width - before[index].width) < .1), 'collapse returns to native geometry')
    matrix.push({ width, wrapped, before, after, frames, reverse })
    if (output) await page.screenshot({ path: path.join(output, `creator-grid-${width}.png`) })
  }
  const rapid = await record(page, [0, 45, 90, 135, 190])
  assert.ok(rapid.every(frame => frame.cards.slice(0, 12).every(card => Math.abs(card.ratio - .75) < .001)))
  assert.ok((await geometry(page)).every(card => card.moving === 0 && card.transform === 'none'))
  assert.equal(await page.locator('#cv-work-list > .cv-card').count(), 72)
  assert.deepEqual(errors, [])
  if (output) fs.writeFileSync(path.join(output, 'creator-grid-frames.json'), JSON.stringify({ matrix, rapid }, null, 2))
})

test('viewport breakpoints, image arrival, append, rebuild, navigation and reduced motion clean up', async t => {
  const { page, errors } = await preview(t)
  for (const width of [900, 640, 480, 400, 901, 1920]) {
    await page.setViewportSize({ width, height: 800 })
    await page.waitForTimeout(350)
    assert.ok((await geometry(page)).every(card => card.moving === 0 && card.transform === 'none'), `settled at ${width}: ${JSON.stringify((await geometry(page)).slice(0, 2))}`)
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `no horizontal overflow at ${width}`)
  }
  await page.evaluate(() => {
    toggleSidebar()
    const grid = document.querySelector('#cv-work-list')
    const card = grid.firstElementChild.cloneNode(true)
    card.querySelector('img').removeAttribute('src')
    grid.appendChild(card)
    setTimeout(() => { card.querySelector('img').src = grid.firstElementChild.querySelector('img').src }, 50)
  })
  await page.waitForTimeout(350)
  assert.ok((await geometry(page)).every(card => card.moving === 0 && card.transform === 'none'))
  await page.evaluate(() => { toggleSidebar(); renderPage() })
  await page.waitForTimeout(350)
  await page.evaluate(() => toggleSidebar())
  await page.waitForTimeout(50)
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const frames = await record(page)
  assert.ok(frames.every(frame => frame.cards.every(card => card.animations === 0 && card.transform === 'none')))
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await page.evaluate(() => { toggleSidebar(); state.page = 'following'; renderPage() })
  await page.waitForTimeout(300)
  assert.equal(await page.locator('#cv-work-list').count(), 0)
  assert.deepEqual(errors, [])
})


test('a large grid measures and animates only visible cards, once per column change', async t => {
  const { page, errors } = await preview(t)
  await page.evaluate(() => {
    const grid = document.querySelector('#cv-work-list'), first = grid.firstElementChild
    const fragment = document.createDocumentFragment()
    for (let i = 72; i < 1200; i++) {
      const card = first.cloneNode(true)
      card.querySelector('img').remove()
      fragment.appendChild(card)
    }
    grid.appendChild(fragment)
    window.scrollTo(0, 2000)
  })
  await page.waitForTimeout(350)
  const result = await page.evaluate(() => new Promise(resolve => {
    const realRect = Element.prototype.getBoundingClientRect, realAnimate = Element.prototype.animate
    const data = { reads: 0, animations: [], frames: [], timing: getComputedStyle(document.querySelector('.shell')).transitionTimingFunction,
      duration: getComputedStyle(document.querySelector('.shell')).transitionDuration }
    Element.prototype.getBoundingClientRect = function (...args) {
      if (this.classList.contains('cv-card')) data.reads++
      return realRect.apply(this, args)
    }
    Element.prototype.animate = function (frames, options) {
      if (this.classList.contains('cv-card')) {
        const rect = realRect.call(this)
        data.animations.push({ top: rect.top, bottom: rect.bottom, frames, duration: options.duration })
      }
      return realAnimate.call(this, frames, options)
    }
    let start = 0
    function frame(now) {
      if (!start) { start = now; toggleSidebar() }
      const sidebar = document.querySelector('.sidebar')
      const width = sidebar.getBoundingClientRect().width
      const transition = sidebar.getAnimations().find(animation => animation.transitionProperty === 'width')
      data.frames.push({ time: now - start, animationTime: transition?.currentTime ?? null, sidebar: width })
      if (now - start < 650) requestAnimationFrame(frame)
      else {
        Element.prototype.getBoundingClientRect = realRect
        Element.prototype.animate = realAnimate
        data.remaining = document.querySelector('#cv-work-list').getAnimations({ subtree: true }).filter(animation => animation.effect.target.classList.contains('cv-card')).length
        resolve(data)
      }
    }
    requestAnimationFrame(frame)
  }))
  assert.equal(result.timing, 'cubic-bezier(0.2, 0, 0, 1)')
  assert.equal(result.duration, '0.25s')
  assert.ok(result.animations.length > 0 && result.animations.length <= 42, 'animation count follows visible cards, not 1200 loaded cards')
  assert.ok(result.reads <= 42, `no per-frame or offscreen card measurement: ${result.reads}`)
  assert.ok(result.animations.every(animation => animation.bottom > 52 && animation.top < 862), 'only viewport-intersecting cards animate')
  assert.ok(result.animations.every(animation => animation.frames.every(frame => !frame.transform.includes('translate'))))
  assert.equal(result.remaining, 0)
  // The last part eases to rest, rather than covering uniform distances then
  // abruptly stopping. Use recorded widths so this checks real CSS motion.
  const end = result.frames.at(-1).sidebar, first = result.frames[0].sidebar
  // The CSS transition starts at style resolution, which can follow the rAF
  // toggle on a busy runner. Sample its own clock rather than the test clock.
  const nearEnd = result.frames.find(frame => frame.animationTime >= 200 && frame.animationTime < 250)
  if (nearEnd) assert.ok(Math.abs(end - nearEnd.sidebar) < Math.abs(end - first) * .06)
  assert.deepEqual(errors, [])
  if (output) fs.writeFileSync(path.join(output, 'creator-grid-performance.json'), JSON.stringify(result, null, 2))
})
