const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')

const app = fs.readFileSync(path.join(__dirname, '../gui/web/app.js'), 'utf8')
const source = app.slice(app.indexOf('function qrLoginLoadingText'), app.indexOf('async function startQrLogin'))

function harness(mode = 'app') {
  let overlay = null, creations = 0
  class Body {
    constructor(html) { this.rebuilds = -1; this.innerHTML = html }
    set innerHTML(html) {
      this.rebuilds++
      this.spinner = html.includes('loading-spinner') ? {} : null
      this.text = html.includes('data-qr-status-text') ? { textContent: '' } : null
      const src = html.match(/<img src="([^"]*)"/)
      this.image = src ? { src: src[1], getAttribute() { return this.src } } : null
    }
    querySelector(selector) {
      if (selector === '[data-qr-status-text]') return this.text
      if (selector === 'img') return this.image
      return null
    }
  }
  const document = {
    activeElement: null,
    querySelector: () => overlay,
    body: { insertAdjacentHTML(position, html) {
      creations++
      const body = new Body(html)
      const actions = { rebuilds: 0, set innerHTML(value) { this.rebuilds++ } }
      overlay = { dataset: {}, body, actions, contains: () => false,
        querySelector(selector) {
          if (selector === '.qr-login-body') return body
          if (selector === '.qr-login-actions') return actions
          return null
        }, remove() { overlay = null } }
    } },
  }
  const context = { document, state: { qrLogin: { mode, status: 'loading', image: '', error: '' } },
    esc: value => String(value), button: () => '<button>retry</button>', focusModal: () => {} }
  vm.createContext(context)
  vm.runInContext(source, context)
  return { context, render: () => context.renderQrLogin(), get overlay() { return overlay },
    get creations() { return creations } }
}

for (const mode of ['app', 'window']) {
  test(`${mode}: changing loading text preserves spinner and overlay identity`, () => {
    const h = harness(mode)
    h.render()
    const overlay = h.overlay, spinner = overlay.body.spinner
    for (const status of ['requesting_qr', 'browser_fallback', 'starting_browser', 'loading_page', 'loading_login_sdk', 'requesting_qr']) {
      h.context.state.qrLogin.status = status
      h.render()
      assert.equal(h.overlay, overlay)
      assert.equal(overlay.body.spinner, spinner)
    }
    assert.equal(overlay.body.rebuilds, 0)
    assert.equal(overlay.actions.rebuilds, 0)
    assert.equal(h.creations, 1)
    assert.match(overlay.body.text.textContent, mode === 'app' ? /申请登录二维码/ : /已打开浏览器/)
  })
}

test('direct QR remains visible while the official SDK initializes', () => {
  const h = harness()
  h.context.state.qrLogin.image = 'data:image/png;base64,direct-code'
  h.context.state.qrLogin.status = 'waiting'
  h.render()
  const image = h.overlay.body.image
  for (const status of ['starting_browser', 'loading_page', 'loading_login_sdk', 'waiting']) {
    h.context.state.qrLogin.status = status
    h.render()
    assert.equal(h.overlay.body.image, image)
    assert.equal(h.overlay.body.spinner, null)
  }
  assert.equal(h.overlay.body.rebuilds, 0)
})

test('QR updates change only the image, while close removes the overlay', () => {
  const h = harness()
  h.render()
  h.context.state.qrLogin.image = 'data:image/png;base64,first'
  h.context.state.qrLogin.status = 'waiting'
  h.render()
  const image = h.overlay.body.image
  const rebuilds = h.overlay.body.rebuilds
  h.context.state.qrLogin.image = 'data:image/png;base64,second'
  h.render()
  assert.equal(h.overlay.body.image, image)
  assert.equal(image.src, 'data:image/png;base64,second')
  assert.equal(h.overlay.body.rebuilds, rebuilds)
  h.context.state.qrLogin = null
  h.render()
  assert.equal(h.overlay, null)
})
