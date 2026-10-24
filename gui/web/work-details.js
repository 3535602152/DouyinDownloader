/* One detail projection for the download list and history. Its responsive
 * role changes in place; there is no separate mobile business implementation. */
(() => {
  const narrow = window.matchMedia('(max-width: 1199px)')
  let current = null, generation = 0, trigger = null
  const host = () => document.getElementById('work-detail-host')
  function syncRole() {
    const node = host(), dialog = node?.querySelector('.work-detail-dialog')
    if (!dialog || node.hidden) return
    dialog.setAttribute('role', narrow.matches ? 'dialog' : 'region')
    if (narrow.matches) dialog.setAttribute('aria-modal', 'true')
    else dialog.removeAttribute('aria-modal')
    document.documentElement.dataset.workDetail = narrow.matches ? 'drawer' : 'pane'
    AppUI.syncModal()
  }
  function body() {
    if (!current) return ''
    const { work, facts, scope, loading, error } = current
    const status = TaskPresentation.describe(work.status || facts.status || (scope === 'history' ? 'downloaded' : 'not_downloaded'))
    const files = Array.isArray(facts.files) ? facts.files : []
    const failure = work.error || work.last_error || facts.error || ''
    const rows = [['作品 ID', work.aweme_id], ['作者', work.author_name || work.author || work.nickname],
      ['发布时间', work.publish_time || work.create_time], ['类型', work.media_type || typeLabels[work.aweme_type]]]
    return `<h3>${esc(work.title || facts.title || '无标题')}</h3><span class="status-badge ${status.tone}">${esc(status.label)}</span>
      <dl>${rows.filter(([, value]) => value).map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl>
      ${failure ? `<div class="notice error" role="alert">${esc(failure)}</div>` : ''}
      <h4>本地文件${files.length ? ` · ${files.length}` : ''}</h4>
      ${loading ? '<p class="setting-desc" role="status">正在读取本地文件…</p>' : error ? `<div class="notice error" role="alert">${esc(error)}</div>${button('重试读取', 'work-details-refresh', 'secondary')}`
        : files.length ? `<ul class="work-files">${files.map(file => `<li><span>${esc(file.path)}</span><small>${esc(TaskPresentation.fileKinds[file.kind] || '附件')} · ${TaskPresentation.bytes(file.size)}</small></li>`).join('')}</ul>`
          : '<p class="setting-desc">暂未找到本地文件。</p>'}
      <div class="work-detail-actions">${files.length ? button('打开文件夹', 'work-details-folder', 'secondary') : ''}
      ${scope === 'link' ? button(status.tone === 'error' || status.tone === 'warning' ? '重试下载' : '下载作品', 'work-details-download', 'primary', state.busy.link ? 'disabled' : '') : ''}</div>`
  }
  function update() {
    const node = host()
    if (!current || !node) return
    if (!node.querySelector('.work-detail-dialog')) node.innerHTML = `<section class="work-detail-dialog" aria-labelledby="work-detail-title" tabindex="-1">
      <header><h2 id="work-detail-title">作品详情</h2><button type="button" class="icon-button" data-ui-dismiss data-action="work-details-close" aria-label="关闭作品详情">×</button></header>
      <div class="work-detail-body"></div></section>`
    const target = node.querySelector('.work-detail-body'), html = body()
    if (target.innerHTML !== html) target.innerHTML = html
    node.hidden = false
    syncRole()
  }
  async function refresh() {
    if (!current) return
    const request = ++generation, id = String(current.work.aweme_id)
    current.loading = !!bridge(); current.error = ''; update()
    const result = await call('work_details', id)
    if (!current || generation !== request) return
    current.loading = false
    if (result) current.facts = result
    else if (bridge()) current.error = '本地详情读取失败，请重试'
    update()
  }
  function open(scope, work, button) {
    current = { scope, work, facts: {}, loading: false, error: '' }
    trigger = button
    refresh()
  }
  function close() {
    generation += 1; current = null
    const node = host(); if (node) { node.hidden = true; node.replaceChildren() }
    delete document.documentElement.dataset.workDetail
    AppUI.syncModal()
    if (trigger?.isConnected && !trigger.closest('[inert]')) trigger.focus({ preventScroll: true })
    trigger = null
  }
  function syncPage(page) {
    if (!current) return
    if (current.scope !== page) close()
    else update()
  }
  async function handle(action, node) {
    if (action === 'work-details-open') {
      const scope = node.dataset.scope
      const list = scope === 'history' ? state.history.items : scope === 'user' ? state.userItems : state.linkItems
      const work = list.find(item => String(item.aweme_id) === node.dataset.id)
      if (work) open(scope, work, node)
    } else if (action === 'work-details-close') close()
    else if (action === 'work-details-refresh') await refresh()
    else if (current && action === 'work-details-folder') {
      if (await call('open_work_directory', String(current.work.aweme_id)) !== true) showToast('文件已移走或文件夹打开失败')
    } else if (current?.scope === 'link' && action === 'work-details-download' && !state.busy.link) {
      const accepted = await call('start_download', formValues(), [String(current.work.aweme_id)])
      if (accepted !== true) showToast('下载未能启动')
    }
  }
  narrow.addEventListener('change', syncRole)
  window.WorkDetails = { handle, syncPage, update, close }
})()
