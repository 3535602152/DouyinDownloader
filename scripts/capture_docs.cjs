/* Reproducible documentation pictures. Mock data only; external requests denied. */
const fs = require('node:fs')
const http = require('node:http')
const path = require('node:path')
const { chromium } = require('playwright')
const root = path.resolve(__dirname, '../gui/web')
const output = path.resolve(__dirname, '../docs/images')
;(async () => {
  const server = http.createServer((req, res) => {
    const file = path.resolve(root, '.' + new URL(req.url, 'http://localhost').pathname)
    if (file !== root && !file.startsWith(root + path.sep)) return res.writeHead(403).end()
    const target = file === root ? path.join(root, 'index.html') : file
    try {
      res.setHeader('Content-Type', { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' }[path.extname(target)] || 'application/octet-stream')
      res.end(fs.readFileSync(target))
    } catch { res.writeHead(404).end() }
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  const browser = await chromium.launch()
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 850 } })
    await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort())
    await page.goto(`http://127.0.0.1:${server.address().port}`)
    await page.locator('#content .panel').first().waitFor()
    await page.evaluate(() => {
      applySettingsState(Object.fromEntries(SettingsUI.fields().map(f => [f.state || f.key, f.default])))
      state.settings.path = 'Downloaded'
      state.forms.path = 'Downloaded'
      state.page = 'settings'; render()
    })
    await page.waitForTimeout(500)
    fs.mkdirSync(output, { recursive: true })
    await page.screenshot({ path: path.join(output, 'settings.png') })
    await page.evaluate(() => {
      state.page = 'link'
      applyEventBatch(Array.from({ length: 4 }, (_, i) => ({ type: 'preview_item', scope: 'link', item: {
        aweme_id: `demo-${i}`, title: ['示例视频 · 日常记录', '示例图文 · 城市漫步', '示例视频 · 旅行片段', '示例音乐'][i],
        author: '演示作者', status: i === 0 ? '已存在' : '待下载', media_type: i === 1 ? 'image' : 'video',
      } })))
      render()
    })
    await page.waitForTimeout(500)
    await page.screenshot({ path: path.join(output, 'downloads.png') })
    console.log('Documentation screenshots captured with synthetic data')
  } finally {
    await browser.close()
    server.closeAllConnections()
    await new Promise(resolve => server.close(resolve))
  }
})().catch(error => { console.error(error); process.exitCode = 1 })
