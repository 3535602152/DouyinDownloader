/* One event router for the existing bridge. Account and task lifecycle
 * checks remain in their shared models; render batching changes only paint. */
function applyEventBatch(events) {
  AppRenderBatch.run(() => events.forEach(applyEvent))
}
function applyEvent(event) {
  if (!event || !event.type) return
  if (applyAccountEvent(event)) return
  if (event.type === 'subscription_rows') {
    state.subscriptions = Array.isArray(event.rows) ? event.rows : []
    if (state.page === 'user') renderPage()
    return
  }
  if (event.type === 'activity') { setActivity(event.key, event.text, { detail: event.detail, running: event.running }); return }
  if (event.type === 'scan_status') { state.scanLine = { text: String(event.text || ''), detail: String(event.detail || ''), running: !!event.running, countdown: Number(event.countdown || 0), at: Date.now() }; renderStatus(); return }
  if (event.type === 'download_status') { state.downloadStatus = event.text || ''; renderStatus(); return }
  if (event.type === 'log') { state.logs.push(event.text); if (state.logs.length > 500) state.logs.shift(); if (state.logOpen) byId('log-text').textContent = state.logs.join(''); return }
  if (event.type === 'risk_event') { if (state.logOpen && state.logTab === 'risk') queueRiskReportRefresh(); return }
  if (event.type === 'busy') {
    state.busy[event.scope] = !!event.busy
    if (event.scope === 'user') syncAccountHome([])
    renderStatus()
    if (state.page === event.scope) renderPage()
    return
  }
  if (event.type === 'task_state') {
    const scope = event.scope
    if (scope && scope in state.busy) {
      if (event.state === 'queued') { state.taskCounts[scope] = (state.taskCounts[scope] || 0) + 1; state.busy[scope] = true }
      else if (event.state === 'running') state.busy[scope] = true
      else if (event.state === 'done' || event.state === 'cancelled' || event.state === 'failed') {
        state.taskCounts[scope] = Math.max(0, (state.taskCounts[scope] || 1) - 1)
        state.busy[scope] = state.taskCounts[scope] > 0
      }
    } else if (event.task === 'qr-login') {
      if (event.state === 'queued') setActivity('login', '等待扫码')
      else if (event.state === 'running') setActivity('login', '请扫码登录')
      else if (['done', 'cancelled', 'failed'].includes(event.state)) setActivity('login', '')
    }
    renderStatus()
    if (scope === 'user') syncAccountHome([])
    if (state.page === scope || (state.page === 'following' && scope === 'monitor')) renderPage()
    return
  }
  if (event.type === 'user_download_done') {
    const failed = Number(event.failed || 0)
    const text = `下载${failed ? '本轮结束' : '完成'}：成功 ${Number(event.success || 0)}，失败 ${failed}，跳过 ${Number(event.skipped || 0)}，共 ${Number(event.total || 0)} 项`
    renderStatus()
    showToast(text)
    return
  }
  if (event.type === 'user_download_cancelled') {
    renderStatus()
    return
  }
  if (event.type === 'preview_item') { const target = event.scope === 'user' ? state.userItems : state.linkItems; if (!target.some(item => String(item.aweme_id) === String(event.item.aweme_id))) target.push(event.item); if (state.page === (event.scope === 'user' ? 'user' : 'link')) renderPage(); return }
  if (event.type === 'row_status') { const target = event.scope === 'user' ? state.userItems : state.linkItems; const row = target.find(item => String(item.aweme_id) === String(event.id)); if (row) row.status = event.status; if (state.page === event.scope) syncItemStatus(event.scope, event.id); return }
  if (event.type === 'preview_done') { state.busy[event.scope] = (state.taskCounts[event.scope] || 0) > 0; renderStatus(); if (state.page === event.scope) renderPage(); return }
  if (event.type === 'preview_cancelled') { state.busy[event.scope] = (state.taskCounts[event.scope] || 0) > 0; renderStatus(); if (state.page === event.scope) renderPage(); return }
  if (event.type === 'progress') { state.progress = event; state.pageDownloads.link = { text: `下载 ${event.completed}/${event.total}`, running: event.completed < event.total, at: Date.now() }; renderStatus(); return }
  if (event.type === 'file_progress') return
  if (event.type === 'done') { if (state.pageDownloads.link) Object.assign(state.pageDownloads.link, { running: false, at: Date.now() }); renderStatus(); showToast('下载完成'); return }
  if (event.type === 'monitors') { applyMonitorSnapshot(event.rows || []); return }
  if (event.type === 'following_progress') {
    const current = state.followingImport
    if (current.phase !== 'loading') return
    if (!followingRequestMatches(current, event)) return
    current.phase = 'loading'; current.loaded = Number(event.loaded || 0); current.pages = Number(event.pages || 0); current.totalHint = Number(event.total_hint || 0)
    setActivity('following', current.totalHint > 0 ? `读取关注 ${current.loaded}/${current.totalHint}` : `读取关注 ${current.loaded}`)
    if (state.page === 'following') renderPage(); return
  }
  if (event.type === 'following_ready') {
    const current = state.followingImport
    if (current.phase !== 'loading') return
    if (!followingRequestMatches(current, event)) return
    const result = event.result || {}
    current.phase = 'ready'; current.items = Array.isArray(result.items) ? result.items : []; current.account = result.account || {}; current.loaded = current.items.length; current.pages = Number(result.pages || current.pages || 0); current.totalHint = Number(result.total_hint || current.totalHint || 0); current.selected = new Set(); current.page = 1; current.error = ''
    setActivity('following', `读取完成 ${current.items.length}`, { running: false }); if (state.page === 'following') renderPage(); return
  }
  if (event.type === 'following_error') {
    const current = state.followingImport
    if (current.phase !== 'loading') return
    if (!followingRequestMatches(current, event)) return
    current.phase = 'error'; current.error = String(event.text || '关注列表读取失败'); setActivity('following', ''); if (state.page === 'following') renderPage(); return
  }
  if (event.type === 'following_cancelled') {
    const current = state.followingImport
    if (current.phase !== 'loading') return
    if (!followingRequestMatches(current, event)) return
    current.phase = 'idle'; current.open = false; current.requestId = ''; current.error = ''; setActivity('following', ''); if (state.page === 'following') renderPage(); return
  }
  if (event.type === 'following_import_error') {
    const current = state.followingImport
    if (String(current.requestId || '') !== String(event.request_id || '')) return
    current.phase = 'ready'; current.error = String(event.text || '关注主播导入失败')
    renderStatus()
    if (current.open && state.page === 'following') renderPage()
    else showToast(current.error)
    return
  }
  if (event.type === 'following_imported') {
    const current = state.followingImport
    if (!current.requestId) current.requestId = String(event.request_id || '')
    if (String(event.request_id || '') !== String(current.requestId || '')) return
    current.phase = 'idle'; current.open = false; current.error = ''
    const added = Number(event.added || 0); const skipped = Number(event.skipped || 0); const invalid = Number(event.invalid || 0)
    setActivity('following', `导入完成 ${added}`, { running: false }); if (state.page === 'following') renderPage()
    showToast(`已导入 ${added} 个${skipped ? `，跳过 ${skipped} 个已存在` : ''}${invalid ? `，忽略 ${invalid} 个无效项` : ''}`)
    return
  }
  if (event.type === 'monitor_added') { applyMonitorAdded(event); return }
  if (event.type === 'monitor_add_failed') {
    removePendingMonitor(event.url)
    const failedUrl = String(event.url || '').trim()
    if (state.monitorAddSubmitting && state.monitorAddSubmittedUrls.includes(failedUrl)) {
      state.monitorAddFailedUrlsDuringSubmit.add(failedUrl)
    }
    if (failedUrl && !String(state.forms.monitorUrls || '').split(/\r?\n/).map(value => value.trim()).includes(failedUrl)) {
      state.forms.monitorUrls = [String(state.forms.monitorUrls || '').trim(), failedUrl].filter(Boolean).join('\n')
    }
    if (state.page === 'following') renderPage()
    showToast(event.text || '主播添加失败')
    return
  }
  if (event.type === 'monitor_refresh') { requestMonitorRefresh(); return }
  if (event.type === 'monitor_delete_done') {
    for (const id of event.sec_uids || []) state.pendingMonitorDeletes.delete(String(id))
    const count = Number(event.creator_count || 0)
    if (!event.delete_local_works) showToast(`已移除 ${count} 个主播`)
    else if (event.files_failed || event.error) showToast(`主播已移除，${Number(event.files_failed || 0)} 个本地文件未能删除`)
    else showToast(`主播已移除，删除了 ${Number(event.files_deleted || 0)} 个本地文件`)
    return
  }
  if (event.type === 'local_validate_progress') { state.busy.localValidate = true; state.localValidate = { ...(state.localValidate || {}), busy: true, scanned: event.scanned, total: event.total }; setActivity('validate', event.total > 0 ? `校验本地文件 ${event.scanned}/${event.total}` : '校验本地文件'); if (state.page === 'following') syncMonitorValidation(); return }
  if (event.type === 'local_validate_done') { const checked = Number(state.localValidate && state.localValidate.total || 0); const lost = Number(event.reset_count || 0); state.busy.localValidate = false; state.localValidate = { busy: false, scanned: 0, total: 0 }; setActivity('validate', `校验完成 ${checked}${lost ? ` 缺失${lost}` : ''}`, { running: false }); if (state.page === 'following') syncMonitorValidation(); showToast('本地文件校验完成'); return }
  if (event.type === 'full_scan_done') { if (state.page === 'following') renderPage(); return }
  if (event.type === 'comments_reset') { state.comments = []; state.images = []; state.selected.images.clear(); if (state.page === 'comments') renderPage(); return }
  if (event.type === 'comment') {
    const comment = event.comment || {}
    const cid = String(comment.cid || '')
    const idx = cid ? state.comments.findIndex(item => String(item.cid || '') === cid) : -1
    if (idx >= 0) state.comments[idx] = comment
    else state.comments.push(comment)
    if (state.page === 'comments') renderPage(); return
  }
  if (event.type === 'comment_image') {
    const image = event.image || {}
    const key = String(image.key || '')
    const idx = key ? state.images.findIndex(item => String(item.key || '') === key) : -1
    if (idx >= 0) state.images[idx] = image
    else state.images.push(image)
    if (state.page === 'comments') renderPage(); return
  }
  if (event.type === 'comment_busy') { state.busy.comments = !!event.busy || (state.taskCounts.comments || 0) > 0; renderStatus(); if (state.page === 'comments') renderPage(); return }
  if (event.type === 'comments_done') { if ((state.taskCounts.comments || 0) === 0) state.busy.comments = false; else state.busy.comments = true; renderStatus(); if (state.page === 'comments') renderPage(); return }
  if (event.type === 'comment_download_progress') { state.pageDownloads.comments = { text: `下载 ${event.current}/${event.total}`, running: event.current < event.total, at: Date.now() }; renderStatus(); return }
  if (event.type === 'comment_download_done') { if (state.pageDownloads.comments) Object.assign(state.pageDownloads.comments, { running: false, at: Date.now() }); if ((state.taskCounts.comments || 0) === 0) state.busy.comments = false; else state.busy.comments = true; renderStatus(); if (state.page === 'comments') renderPage(); showToast(`已保存 ${event.success} 张图片${event.failed ? `，失败 ${event.failed} 张` : ''}`); return }
  if (event.type === 'comment_zoom') {
    if (event.ok && event.src) {
      openCommentImagePreview([event.src])
    } else if (event.text) {
      state.imagePreviewReturnFocus = null
      showToast(event.text)
    }
    return
  }
  if (event.type === 'accounts') {
    state.settings.accounts = event.accounts || []
    const main = state.settings.accounts.find(item => item.is_main)
    state.settings.cookie_configured = !!event.logged_in && !(main && main.expired)
    state.settings.cookie_invalid = !!(main && main.expired)
    if (state.page === 'settings') renderPage()
    return
  }
  if (event.type === 'account_profile') { state.settings.account_profile = event.profile || {}; if (state.page === 'settings') renderPage(); return }
  if (event.type === 'account_profile_auth_error') { state.settings.cookie_invalid = true; state.settings.cookie_configured = false; if (state.page === 'settings') renderPage(); return }
  if (event.type === 'login_qr') {
    const login = state.qrLogin
    if (!login || login.status === 'error') return
    if (event.image) {
      login.image = event.image
      if (login.status !== 'scanned') login.status = 'waiting'
    }
    if (event.status) login.status = event.status
    renderQrLogin()
    return
  }
  if (event.type === 'login') {
    if (event.ok === 'queued' || event.ok === null) {
      setActivity('login', '等待扫码')
    } else if (event.ok) {
      setActivity('login', '')
      state.qrLogin = null
      renderQrLogin()
      state.settings.cookie_configured = event.logged_in !== false
      state.settings.cookie_invalid = false
      state.settings.cookie_count = event.count
      showToast(event.nickname ? `已登录：${event.nickname}` : '登录已更新')
      if (state.page === 'settings') renderPage()
      if (state.settings.cookie_configured) showResumeUserDownloadPrompt()
    } else if (state.qrLogin) {
      setActivity('login', '')
      state.qrLogin = { ...state.qrLogin, status: 'error', error: event.text || '' }
      renderQrLogin()
    } else { setActivity('login', ''); showToast(event.text) }
    return
  }
  if (event.type === 'settings_saved') { receiveSettings(event.state || state.settings); return }
  if (event.type === 'user_url') { state.forms.userUrl = `https://www.douyin.com/user/${event.sec_uid}`; state.page = 'user'; render(); return }
  if (event.type === 'error') {
    state.busy[event.scope] = (state.taskCounts[event.scope] || 0) > 0
    if (event.scope === 'localValidate') state.localValidate = { busy: false, scanned: 0, total: 0 }
    if (event.scope === 'monitor') state.errors.monitor = String(event.text || '监控列表读取失败')
    if (event.scope === 'history') state.errors.history = String(event.text || '下载记录读取失败')
    showToast(event.text)
    renderStatus()
    const pageForScope = event.scope === 'localValidate' || event.scope === 'monitor' ? 'following' : event.scope
    if (state.page === pageForScope) renderPage()
  }
}
