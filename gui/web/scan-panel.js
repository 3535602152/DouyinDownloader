// Scan view: hovering the footer's left lane opens, in the shared footer
// panel (footer-panel.js), the scans that are running or waiting (队列) and
// the sources whose last scan failed (失败).
(() => {
  const ROW_LIMIT = 30
  const POLL_MS = 1000
  let open = false
  let tab = 'queue'
  let data = null
  let pollTimer = null
  let loading = false

  const lane = () => document.getElementById('status-scan-lane')
  const panel = () => document.getElementById('scan-panel')

  function clock(seconds) {
    if (!seconds) return ''
    const date = new Date(seconds * 1000)
    const pad = value => String(value).padStart(2, '0')
    const today = new Date()
    const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`
    return date.toDateString() === today.toDateString() ? time : `${date.getMonth() + 1}-${pad(date.getDate())} ${time}`
  }

  function queueRow(row) {
    const running = row.state === 'running'
    return `<div class="dl-row sp-row${running ? ' running' : ''}" role="listitem" data-ui-key="${esc(row.key || row.id || `${row.kind}:${row.name}`)}">`
      + `<div class="dl-name"><span class="dl-title" title="${esc(row.name)}">${esc(row.name)}</span></div>`
      + `<div class="dl-size">${esc(row.kind)}</div>`
      + `<div class="sp-state">${running ? `<b>${esc(row.progress)}</b>` : `<span>${esc(row.progress)}</span>`}</div>`
      + '</div>'
  }

  // A readable reason when the error is recognised, else the raw error; one
  // line either way, the full error on hover.
  function failureRow(row) {
    const text = row.reason || row.error
    const count = row.count > 1 ? `<em>${row.count} 次</em>` : ''
    return `<div class="dl-row sp-row" role="listitem" data-ui-key="${esc(row.key || row.id || `${row.kind}:${row.name}`)}">`
      + `<div class="dl-name"><span class="dl-title" title="${esc(row.name)}">${esc(row.name)}</span>`
      + `<span class="dl-creator">${esc(clock(row.time))}</span></div>`
      + `<div class="dl-size">${esc(row.kind)}</div>`
      + `<div class="sp-state sp-error">${count}<span title="${esc(row.error)}">${esc(text)}</span></div>`
      + '</div>'
  }

  function render() {
    const node = panel()
    if (!node || !data) return
    const failures = Array.isArray(data.failures) ? data.failures : []
    const rows = Array.isArray(data.rows) ? data.rows : []
    node.querySelectorAll('.sp-tab').forEach(button => {
      const selected = button.dataset.tab === tab
      button.setAttribute('aria-selected', selected ? 'true' : 'false')
      const count = button.dataset.tab === 'failed' ? Number(data.failure_count) || 0
        : (Number(data.running) || 0) + (Number(data.waiting) || 0)
      button.querySelector('.sp-tab-count').textContent = count ? ` ${count}` : ''
    })
    node.querySelector('.sp-head-note').textContent = data.paused === '风控暂停' && data.countdown > 0
      ? `风控 ${data.countdown}s` : data.paused || ''
    const columns = node.querySelectorAll('.dl-columns span')
    columns[2].textContent = tab === 'failed' ? '原因' : '状态'
    const list = node.querySelector('.dl-list')
    let html
    if (tab === 'failed') {
      html = failures.slice(0, ROW_LIMIT).map(failureRow).join('') || '<div class="dl-empty">没有失败的扫描</div>'
    } else {
      html = rows.slice(0, ROW_LIMIT).map(queueRow).join('') || '<div class="dl-empty">队列为空</div>'
      const remaining = Number(data.remaining) || 0
      if (remaining > 0) html += `<div class="dl-more">还有 ${remaining} 个等待扫描</div>`
      if (Number(data.later) > 0) html += `<div class="dl-more">其余 ${Number(data.later)} 个 下次 ${esc(clock(data.next_at))}</div>`
    }
    AppUI.reconcileRows(list, html)
  }

  async function refresh() {
    if (!open || loading) return
    loading = true
    try {
      const next = await call('scan_board', ROW_LIMIT)
      if (open && next) { data = next; render() }
    } finally {
      loading = false
    }
  }

  function activate() {
    if (open) return
    open = true
    refresh()
    pollTimer = setInterval(refresh, POLL_MS)
  }

  function deactivate() {
    open = false
    clearInterval(pollTimer)
    pollTimer = null
  }

  function bind() {
    const node = panel()
    if (!node || node.dataset.panelBound) return
    node.dataset.panelBound = '1'
    FooterPanel.register('scan', {
      lane, view: panel, activate, deactivate,
      anchor: () => document.getElementById('status-text'),
      available: () => true,
    })
    node.addEventListener('click', event => {
      const button = event.target.closest('.sp-tab')
      if (!button || button.dataset.tab === tab) return
      tab = button.dataset.tab
      render()
    })
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind)
  else bind()
  window.ScanPanel = { show: () => FooterPanel.show('scan'), hide: () => FooterPanel.close('scan'), render: next => { data = next; render() } }
})()
