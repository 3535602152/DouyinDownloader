/* Real packaged WebView2 check. Every launch uses only the specified test profile. */
const assert = require('node:assert/strict')
const { spawn } = require('node:child_process')
const fs = require('node:fs')
const net = require('node:net')
const path = require('node:path')
const { chromium } = require('playwright')
const pause = ms => new Promise(resolve => setTimeout(resolve, ms))
;(async () => {
  const [exeArg, dataArg, outputArg] = process.argv.slice(2)
  assert.ok(exeArg && dataArg, 'usage: node scripts/smoke_exe.cjs EXE ISOLATED_DATA_DIR [SCREENSHOT]')
  const exe = path.resolve(exeArg), data = path.resolve(dataArg)
  assert.ok(!data.toLowerCase().includes('appdata'), 'Use an explicit test directory outside AppData')
  fs.mkdirSync(data, { recursive: true })
  for (let attempt = 0; attempt < 2; attempt++) {
    const server = net.createServer()
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
    const port = server.address().port
    await new Promise(resolve => server.close(resolve))
    const child = spawn(exe, [], { windowsHide: true, env: { ...process.env,
      DOUYIN_DOWNLOADER_DATA_DIR: data,
      WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: `--remote-debugging-port=${port} --remote-debugging-address=127.0.0.1`,
    } })
    let browser
    try {
      for (let i = 0; i < 160; i++) {
        if (child.exitCode !== null) throw new Error(`EXE exited early: ${child.exitCode}`)
        try { browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`, { timeout: 1000 }); break } catch { await pause(250) }
      }
      assert.ok(browser, 'Packaged WebView2 debug transport started')
      let page
      for (let i = 0; i < 120; i++) {
        page = browser.contexts().flatMap(c => c.pages()).find(p => /index\.html/.test(p.url()))
        if (page) break
        await pause(250)
      }
      assert.ok(page, 'Packaged local page loaded')
      await page.waitForFunction(() => typeof state !== 'undefined' && state.startup === '', { timeout: 30000 })
      assert.equal(await page.locator('.sidebar .nav button').count(), 6)
      if (attempt) assert.equal(await page.evaluate(() => localStorage.getItem('packaged-release-smoke')), 'persistent')
      await page.evaluate(() => localStorage.setItem('packaged-release-smoke', 'persistent'))
      await page.locator('[data-page="settings"]').click()
      await page.locator('.settings-group').first().waitFor()
      if (outputArg && attempt) await page.screenshot({ path: path.resolve(outputArg) })
      await page.evaluate(() => { window.pywebview.api.window_command('close') }).catch(() => {})
      for (let i = 0; i < 40 && child.exitCode === null; i++) await pause(250)
      assert.equal(child.exitCode, 0, 'Packaged application closes cleanly')
    } finally {
      if (browser) await browser.close().catch(() => {})
      if (child.exitCode === null) child.kill()
    }
  }
  console.log(JSON.stringify({ ok: true, nativeUI: true, restartPersistence: true }))
})().catch(error => { console.error(error); process.exitCode = 1 })
