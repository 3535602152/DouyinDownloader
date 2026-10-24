// Download view: hovering the footer's "下载 xx/yy" opens the current page's
// running and queued works in the shared footer panel (footer-panel.js).
// Only the first ROW_LIMIT rows are rendered; the rest is summarized by a
// trailing "… 还有 N 个" row so a 20k-work queue never reaches the DOM.
// The background queue has three tabs: 下载 (the followed creators' works),
// 用户 (like/collect works, one group per subscription) and 评论 (the comment
// sync).  Switching tabs never makes the frame shorter while it stays open,
// so the pointer is not left outside the frame (which would close it).
(() => {
  const ROW_LIMIT = 30
  const POLL_MS = 1000
  let open = false
  let pollTimer = null
  let loading = false
  let tab = 'download'
  let downloadData = null
  let userData = null
  let commentData = null
  // 用户 tab: groups opened by a click show one running and one queued work;
  // groups also in ``fullGroups`` list their queue like the 下载 tab.
  const openGroups = new Set()
  const fullGroups = new Set()

  const lane = () => document.getElementById('status-download-lane')
  const panel = () => document.getElementById('download-panel')
  // The account pages show their own download; a page's own running download
  // (links, comment images) has no queue to list; otherwise the queue.
  const surface = () => state.page === 'user' || (state.page === 'streamer' && state.streamer.catalogue) ? 'user'
    : pageDownload() ? '' : 'monitor'

  const STATE_LABELS = TaskPresentation.labels
  const formatEta = TaskPresentation.eta

  // Every row has the same three cells and height; a queued work simply has
  // an empty bar and no size yet.
  function rowHtml(row, user) {
    const running = ['downloading', 'large', 'extras'].includes(row.state)
    const title = row.title || `作品 ${row.aweme_id}`
    const total = Number(row.total) || 0
    const done = Math.min(Number(row.done) || 0, total || Number(row.done) || 0)
    const progress = TaskPresentation.progress(done, total)
    const percent = progress.percent || 0
    const rate = running ? Number(row.rate) || 0 : 0
    const size = user ? row.aweme_id : running && total > 0
      ? `${formatMonitorBytes(done)}/${formatMonitorBytes(total)}`
      : '—'
    let status = STATE_LABELS[row.state] || ''
    if (running && ((user && (done > 0 || total > 0)) || (!total && done > 0))) {
      const bytes = total > 0 ? `${formatMonitorBytes(done)}/${formatMonitorBytes(total)}`
        : `已接收 ${formatMonitorBytes(done)}`
      status = `<span title="${esc(row.fileName || '')}">${esc(bytes)}</span><b>${esc(formatMonitorBytes(rate))}/s</b>`
    } else if (running && total > 0) {
      const eta = total > done ? formatEta((total - done) / rate) : ''
      // A large video in its own lane is shown like any other download.
      const tag = row.state === 'extras' ? '<em>补附件</em>' : ''
      status = `${tag}<b>${esc(formatMonitorBytes(rate))}/s</b><span>${esc(eta)}</span>`
    } else {
      status = `<span>${esc(running && !user && !total ? '准备中' : status)}</span>`
    }
    return `<div class="dl-row${running ? ' running' : ''}" role="listitem" data-ui-key="${esc(row.aweme_id)}">`
      + `<div class="dl-name"><span class="dl-title" title="${esc(title)}">${esc(title)}</span>`
      + `<span class="dl-creator">${esc(row.creator || '')}</span></div>`
      + `<div class="dl-size">${esc(size)}</div>`
      + `<div class="dl-status"><div class="dl-status-text">${status}</div>`
      // Running without a size yet (fetching the work's detail): a sliding bar.
      + (running && !total
        ? '<div class="dl-bar pending" role="progressbar" aria-valuemin="0" aria-valuemax="100"><i></i></div></div>'
        : `<div class="dl-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" ${running && progress.known ? `aria-valuenow="${percent.toFixed(0)}"` : ''}><i style="width:${percent.toFixed(1)}%"></i></div></div>`)
      + '</div>'
  }

  const PHASES = {
    reading: '读取中', gap: '下一个作品', paused: '评论风控暂停',
    idle: '没有待读的作品', login: '未登录，登录后继续', starting: '启动中',
  }

  function countdown(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0))
    if (!value) return ''
    if (value < 60) return `${value}s`
    if (value < 3600) return `${Math.floor(value / 60)}分${String(value % 60).padStart(2, '0')}秒`
    return `${Math.floor(value / 3600)}小时${String(Math.floor(value % 3600 / 60)).padStart(2, '0')}分`
  }

  function commentRow(row, limit) {
    const running = row.state === 'running'
    const title = row.title || `作品 ${row.aweme_id}`
    const total = Number(row.comment_count)
    const size = total >= 0 ? `${formatMonitorCount(total)} 条` : '—'
    // The second pass reads every comment: its goal is the work's whole count.
    const full = row.stage === 'full'
    const goal = full ? Math.max(0, total) : total >= 0 ? Math.min(limit, total) : limit
    const read = Number(row.read) || 0
    const percent = goal > 0 && (running || full) ? Math.min(100, (read / goal) * 100) : 0
    const status = running ? `<b>已读 ${goal > 0 ? `${read}/${goal}` : read}</b>`
      : '<span>等待中</span>'
    return `<div class="dl-row${running ? ' running' : ''}" role="listitem" data-ui-key="${esc(row.aweme_id)}">`
      + `<div class="dl-name"><span class="dl-title" title="${esc(title)}">${esc(title)}</span>`
      + `<span class="dl-creator">${esc(row.creator || '')}</span></div>`
      + `<div class="dl-size">${esc(size)}</div>`
      + `<div class="dl-status"><div class="dl-status-text">${status}</div>`
      + `<div class="dl-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent.toFixed(0)}"><i style="width:${percent.toFixed(1)}%"></i></div></div>`
      + '</div>'
  }

  function renderTabs(node, user) {
    const tabs = node.querySelector('.dl-tabs')
    tabs.hidden = user
    node.querySelector('.dl-head strong').hidden = !user
    tabs.querySelectorAll('.sp-tab').forEach(button => {
      button.setAttribute('aria-selected', button.dataset.tab === tab ? 'true' : 'false')
      const data = button.dataset.tab === 'comments' ? commentData
        : button.dataset.tab === 'users' ? userData : downloadData
      const count = data ? Number(data.pending) || 0 : 0
      button.querySelector('.sp-tab-count').textContent = count ? ` ${count}` : ''
    })
  }

  function renderComments() {
    const node = panel()
    const data = commentData
    if (!node || !data) return
    renderTabs(node, false)
    node.setAttribute('aria-label', '评论队列')
    node.querySelector('.dl-columns span').textContent = '作品'
    node.querySelector('.dl-columns span:nth-child(2)').textContent = '评论数'
    const phase = PHASES[data.phase] || ''
    const left = countdown(data.countdown)
    const limit = Number(data.limit) || 50
    const note = node.querySelector('.dl-head-note')
    note.hidden = false
    note.textContent = [left ? `${phase} ${left}` : phase,
      `本小时 ${Number(data.hour_used) || 0} 次`].filter(Boolean).join(' · ')
    node.querySelector('.dl-head-rate').hidden = true
    node.querySelector('.dl-head-count').hidden = true
    const rebuild = node.querySelector('[data-dl-rebuild]')
    if (rebuild) rebuild.hidden = true
    const rows = Array.isArray(data.rows) ? data.rows.slice(0, ROW_LIMIT) : []
    let html = rows.map(row => commentRow(row, limit)).join('')
      || '<div class="dl-empty">没有待读评论的作品</div>'
    const remaining = Number(data.remaining) || 0
    if (remaining > 0) html += `<div class="dl-more">还有 ${remaining} 个作品等待读评论</div>`
    const list = node.querySelector('.dl-list')
    AppUI.reconcileRows(list, html)
  }

  const KIND_LABELS = { like: '喜欢', collect: '收藏' }

  function groupHtml(group) {
    const id = String(group.id)
    const expanded = openGroups.has(id)
    const full = expanded && fullGroups.has(id)
    const rows = Array.isArray(group.rows) ? group.rows : []
    const running = rows.filter(row => row.state !== 'queued')
    const queued = rows.filter(row => row.state === 'queued')
    const rate = running.reduce((sum, row) => sum + (Number(row.rate) || 0), 0)
    const status = running.length
      ? `<b>下载中${running.length > 1 ? ` ${running.length}` : ''}</b><span>${esc(formatMonitorBytes(rate))}/s</span>`
      : '<span>排队中</span>'
    let html = `<button type="button" class="dl-row dl-group${expanded ? ' open' : ''}" role="listitem" data-ui-key="group:${esc(id)}" data-dl-group="${esc(id)}" aria-expanded="${expanded}">`
      + `<div class="dl-name"><span class="dl-title" title="${esc(group.name)}"><i class="dl-chevron" aria-hidden="true">›</i>${esc(group.name || '未知用户')}</span>`
      + `<span class="dl-creator">${esc(KIND_LABELS[group.kind] || '订阅')}</span></div>`
      + `<div class="dl-size">${esc(formatMonitorCount(Number(group.remaining) || 0))}</div>`
      + `<div class="dl-status"><div class="dl-status-text">${status}</div></div></button>`
    if (!expanded) return html
    const shown = full ? rows.slice(0, ROW_LIMIT) : [...running.slice(0, 1), ...queued.slice(0, 1)]
    html += shown.map(row => rowHtml(row, false).replace('class="dl-row', 'class="dl-row dl-child')).join('')
      || '<div class="dl-empty dl-child">没有正在下载的作品</div>'
    const hidden = Math.max(0, (Number(group.remaining) || 0) - shown.length)
    if (full && hidden > 0) html += `<div class="dl-more dl-child">还有 ${hidden} 个作品排队中</div>`
    if (full || hidden > 0) {
      html += `<button type="button" class="dl-group-all" data-ui-key="all:${esc(id)}" data-dl-group-all="${esc(id)}">${full ? '收起' : '全部展开'}</button>`
    }
    return html
  }

  function renderUsers() {
    const node = panel()
    const data = userData
    if (!node || !data) return
    renderTabs(node, false)
    node.setAttribute('aria-label', '用户下载队列')
    node.querySelector('.dl-columns span').textContent = '用户'
    node.querySelector('.dl-columns span:nth-child(2)').textContent = '剩余作品'
    node.querySelector('.dl-head-note').hidden = true
    const groups = Array.isArray(data.groups) ? data.groups : []
    const rate = groups.flatMap(group => group.rows || [])
      .reduce((sum, row) => sum + (row.state !== 'queued' ? Number(row.rate) || 0 : 0), 0)
    node.querySelector('.dl-head-rate').hidden = false
    node.querySelector('.dl-head-rate').textContent = `${formatMonitorBytes(rate)}/s`
    const count = node.querySelector('.dl-head-count')
    count.hidden = false
    count.textContent = data.pending ? `待下载 ${data.pending}` : ''
    const rebuild = node.querySelector('[data-dl-rebuild]')
    if (rebuild) rebuild.hidden = true
    const html = groups.map(groupHtml).join('') || '<div class="dl-empty">没有要下载的喜欢或收藏</div>'
    AppUI.reconcileRows(node.querySelector('.dl-list'), html)
  }

  function render(data, user = false) {
    const node = panel()
    if (!node) return
    if (!user) downloadData = data
    renderTabs(node, user)
    if (!user && tab === 'comments') { renderComments(); return }
    if (!user && tab === 'users') { renderUsers(); return }
    node.querySelector('.dl-head-note').hidden = true
    node.querySelector('.dl-head-rate').hidden = false
    node.querySelector('.dl-head-count').hidden = false
    const rows = Array.isArray(data.rows) ? data.rows.slice(0, ROW_LIMIT) : []
    const progress = data.progress || {}
    const rate = rows.reduce((sum, row) => sum + (Number(row.rate) || 0), 0)
    const title = user ? `${data.modeLabel || '用户作品'}下载队列` : '下载队列'
    node.setAttribute('aria-label', title)
    node.querySelector('.dl-head strong').textContent = title
    node.querySelector('.dl-columns span').textContent = user ? '作品' : '名称'
    node.querySelector('.dl-columns span:nth-child(2)').textContent = user ? '作品 ID' : '大小'
    // Speed sits left of the count in a fixed-width, right-aligned slot so
    // neither value shifts as digits change; an idle queue reads "0 B/s".
    node.querySelector('.dl-head-rate').textContent = user
      ? data.hasTransfers ? `${formatMonitorBytes(rate)}/s`
        : data.scanState === 'scanning' ? '扫描中' : data.scanState === 'idle' ? '尚未开始' : '扫描已结束'
      : data.login_pause > 0 ? `登录失效 · ${data.login_pause}秒后重试` : `${formatMonitorBytes(rate)}/s`
    // Clearing the queue concerns the background queue, not a page's own download.
    const rebuild = node.querySelector('[data-dl-rebuild]')
    if (rebuild) rebuild.hidden = user
    node.querySelector('.dl-head-count').textContent = user || progress.total
      ? `下载 ${progress.done}/${progress.total}${!user && data.retry ? ` · 待重试 ${data.retry}` : ''}` : ''

    const list = node.querySelector('.dl-list')
    let html = rows.map(row => rowHtml(row, user)).join('')
    if (!rows.length) html = '<div class="dl-empty">队列为空</div>'
    const remaining = Number(data.remaining) || 0
    if (remaining > 0) html += `<div class="dl-more">还有 ${remaining} 个作品排队中</div>`
    // Patch the bounded list so progress nodes and scroll position survive.
    AppUI.reconcileRows(list, html)
  }

  function userBoard() {
    const task = currentUserDownloadTask()
    const owner = task ? task.owner : String(state.userAccount && state.userAccount.sec_uid || '')
    const mode = task ? task.mode : String(state.page === 'streamer' ? state.streamer.contentMode : state.forms.userMode)
    const catalogue = state.streamer.catalogue
    const works = catalogue && catalogue.owner === owner && catalogue.mode === mode ? catalogue.byId : null
    const running = []
    const queued = []
    for (const id of task && task.itemOrder || []) {
      const itemState = task.items[id]
      if (itemState !== 'downloading' && itemState !== 'queued') continue
      const work = works && works.get(id)
      const meta = task.itemMeta[id] || {}
      const file = task.itemFiles[id] || {}
      const liveRate = Date.now() - (file.updatedAt || 0) < 5000 ? file.rate || 0 : 0
      const row = { aweme_id: id, state: itemState, title: meta.title || work && work.title || `作品 ${id}`,
        creator: meta.creator || work && work.author_profile && work.author_profile.nickname
          || state.userAccount && state.userAccount.nickname || '',
        done: file.written || 0, total: file.expected || 0, rate: liveRate, fileName: file.name || '' }
      if (itemState === 'downloading' && running.length < ROW_LIMIT) running.push(row)
      else if (itemState === 'queued' && queued.length < ROW_LIMIT) queued.push(row)
    }
    const rows = [...running, ...queued].slice(0, ROW_LIMIT)
    return { rows, remaining: Math.max(0, (task ? task.downloading + task.queued : 0) - rows.length),
      progress: { done: task ? task.download_done : 0, total: task ? task.download_total : 0 },
      scanState: task ? task.scan_state : 'idle', hasTransfers: running.some(row => row.fileName),
      modeLabel: modeLabels[mode] }
  }

  async function refresh() {
    if (!open) return
    const currentSurface = surface()
    if (!currentSurface) { FooterPanel.close('download'); return }
    if (currentSurface === 'user') { render(userBoard(), true); return }
    if (loading) return
    loading = true
    try {
      const full = [...fullGroups].filter(id => openGroups.has(id)).map(Number)
      const [data, users, comments] = await Promise.all([
        call('monitor_download_board', ROW_LIMIT), call('user_download_board', ROW_LIMIT, full),
        call('comment_board', ROW_LIMIT)])
      if (comments) commentData = comments
      if (users) userData = users
      if (open && surface() === currentSurface && data) render(data)
    } finally {
      loading = false
    }
  }

  // Switching tabs keeps the frame at least as tall as it is now (it may
  // still grow); the hold ends when the frame closes or shows the scan view.
  function switchTab(next) {
    const node = panel()
    if (!node || next === tab) return
    node.style.minHeight = `${node.offsetHeight}px`
    tab = next
    if (tab === 'comments' && commentData) renderComments()
    else if (tab === 'users' && userData) renderUsers()
    else if (downloadData) render(downloadData)
    refresh()
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
    const node = panel()
    if (node) node.style.minHeight = ''
  }

  async function rebuildBacklog(event) {
    const button = event.currentTarget
    if (!window.confirm('清空所有待下载的作品，并重新全量扫描全部主播和订阅？\n\n'
      + '已下载和正在下载的作品不受影响；扫描到的作品会重新排队下载。')) return
    button.disabled = true
    try {
      const dropped = await call('rebuild_download_backlog')
      showToast(`已清空 ${Number(dropped) || 0} 个待下载作品，开始重新扫描`)
      refresh()
    } finally {
      button.disabled = false
    }
  }

  function bind() {
    const node = panel()
    if (!node || node.dataset.panelBound) return
    node.dataset.panelBound = '1'
    FooterPanel.register('download', {
      lane, view: panel, activate, deactivate,
      anchor: () => document.getElementById('status-download'),
      available: () => Boolean(surface()),
    })
    const rebuild = node.querySelector('[data-dl-rebuild]')
    if (rebuild) rebuild.addEventListener('click', rebuildBacklog)
    node.querySelector('.dl-tabs').addEventListener('click', event => {
      const button = event.target.closest('.sp-tab')
      if (button) switchTab(button.dataset.tab)
    })
    node.querySelector('.dl-list').addEventListener('click', event => {
      const all = event.target.closest('[data-dl-group-all]')
      const head = all ? null : event.target.closest('[data-dl-group]')
      if (!all && !head) return
      if (all) {
        const id = all.dataset.dlGroupAll
        if (fullGroups.has(id)) fullGroups.delete(id)
        else fullGroups.add(id)
      } else {
        const id = head.dataset.dlGroup
        if (openGroups.has(id)) { openGroups.delete(id); fullGroups.delete(id) }
        else openGroups.add(id)
      }
      renderUsers()
      refresh()
    })
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind)
  else bind()
  window.DownloadPanel = { show: () => FooterPanel.show('download'), hide: () => FooterPanel.close('download'), render, formatEta }
})()
