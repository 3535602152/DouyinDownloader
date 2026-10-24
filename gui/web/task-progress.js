/* One footer progress contract; monitor and account tasks keep separate snapshots. */
(function (root) {
  'use strict'
  function newer(current, next, id = 'task_id') {
    if (!current || String(current[id]) !== String(next[id])) return true
    return Number.isSafeInteger(next.revision) && next.revision > current.revision
  }
  function formatLanes(scan, download) {
    return {
      scan: scan.bare ? scan.label
        : `${scan.label} ${scan.done}${scan.total === null ? '' : `/${scan.total}`}${scan.suffix || ''}`,
      download: `${download.label} ${download.done} / ${download.total}`,
      scanTone: scan.tone,
      downloadTone: download.tone,
      detail: scan.detail || '',
    }
  }
  const countKeys = ['scan_total', 'scan_done', 'scan_failed', 'download_total', 'download_done', 'download_success', 'download_failed']
  function valid(task) {
    return task && Number.isSafeInteger(task.task_id) && task.task_id > 0
      && Number.isSafeInteger(task.revision) && task.revision >= 0
      && countKeys.every(key => Number.isSafeInteger(task[key]) && task[key] >= 0)
      && task.scan_done <= task.scan_total && task.scan_failed <= task.scan_done
      && task.download_done <= task.download_total
      && task.download_done === task.download_success + task.download_failed
  }
  function accept(current, next) {
    if (!valid(next)) return current
    if (current && (next.task_id < current.task_id
      || !newer(current, next))) return current
    if (current && next.task_id === current.task_id
      && (next.scan_total !== current.scan_total || next.scan_done < current.scan_done
        || next.download_total < current.download_total || next.download_done < current.download_done)) return current
    return { ...next }
  }
  function active(task) {
    return !!task && task.status !== 'interrupted'
      && task.scan_done < task.scan_total
  }
  function format(task) {
    if (!task) return null
    const scanning = task.scan_done < task.scan_total
    const interrupted = task.status === 'interrupted'
    const scanLabel = interrupted ? '全量扫描已中断' : scanning ? '全量扫描' : '全量扫描完成'
    const currentCreator = String(task.current_creator_name || '').trim()
    const currentScanned = Number.isSafeInteger(task.current_scanned) && task.current_scanned >= 0
      ? task.current_scanned : 0
    const scanDetail = currentCreator ? `${currentCreator} ${currentScanned}` : ''
    return formatLanes({ label: scanLabel, done: task.scan_done, total: task.scan_total,
      suffix: task.scan_failed ? ` 失败${task.scan_failed}` : '',
      tone: interrupted || task.scan_failed ? 'bad' : scanning ? 'active' : 'ok',
      detail: interrupted ? (task.error || '') : scanning ? scanDetail : '',
    }, { label: '下载', done: task.download_done, total: task.download_total,
      tone: active(task) ? 'active' : task.download_failed || interrupted ? 'bad' : 'ok' })
  }
  const api = { accept, active, format }
  if (typeof module !== 'undefined' && module.exports) module.exports = api
  root.FullScanProgress = api

  const userStates = new Set(['idle', 'scanning', 'completed', 'failed', 'cancelled'])
  function base(taskId, revision, kind = 'download') {
    return { task_id: String(taskId), revision, kind, scan_state: 'scanning', scanned: 0,
      total_hint: 0, accepted: 0, pages: 0, error: '', download_state: 'scanning',
      download_total: 0, queued: 0, downloading: 0, download_done: 0,
      success: 0, failed: 0, skipped: 0, cancelled: 0, items: {}, itemOrder: [], itemMeta: {}, itemFiles: {} }
  }
  function validUser(task) {
    const counts = ['revision', 'scanned', 'total_hint', 'accepted', 'pages', 'download_total',
      'queued', 'downloading', 'download_done', 'success', 'failed', 'skipped', 'cancelled']
    return task && task.task_id && userStates.has(task.scan_state)
      && counts.every(key => Number.isSafeInteger(task[key]) && task[key] >= 0)
      && task.download_done === task.success + task.failed + task.cancelled
  }
  function acceptUser(current, next) {
    if (!validUser(next) || !newer(current, next)) return current
    if (current && current.task_id === next.task_id
      && (next.scanned < current.scanned || next.accepted < current.accepted
        || next.download_total < current.download_total || next.download_done < current.download_done)) return current
    return next
  }
  function browse(current, event, totalHint = 0) {
    const taskId = `browse:${event.request_id || ''}`
    if (current && current.task_id === taskId && current.scan_state !== 'scanning') return current
    const next = current && current.task_id === taskId ? { ...current } : base(taskId, 0, 'browse')
    next.revision += 1
    next.scanned = Math.max(next.scanned, Number(event.scanned || 0))
    next.pages = Math.max(next.pages, Number(event.pages || 0))
    next.total_hint = Math.max(next.total_hint, Number(event.total_hint || totalHint || 0))
    if (event.type === 'user_catalogue_state') {
      next.scan_state = event.state === 'done' ? 'completed' : String(event.state || 'failed')
      next.error = String(event.error || '')
    }
    return acceptUser(current, next)
  }
  function download(current, event) {
    const generation = Number(event.generation)
    const revision = Number(event.revision)
    if (!Number.isSafeInteger(generation) || generation < 1 || !Number.isSafeInteger(revision)) return current
    const taskId = `download:${generation}`
    if (current && current.kind === 'download') {
      const oldGeneration = Number(String(current.task_id).split(':')[1])
      if (generation < oldGeneration) return current
    }
    if (current && current.task_id === taskId && !newer(current, { task_id: taskId, revision })) return current
    const next = current && current.task_id === taskId
      ? { ...current, items: current.items, itemOrder: current.itemOrder,
        itemMeta: current.itemMeta, itemFiles: current.itemFiles } : base(taskId, 0)
    next.revision = revision
    next.owner = String(event.sec_uid || '')
    next.mode = String(event.mode || '')
    if (event.type === 'user_download_scan') {
      next.scan_state = String(event.scan_state || next.scan_state)
      next.scanned = Number(event.scanned || 0)
      next.accepted = Number(event.accepted || 0)
      next.pages = Number(event.pages || 0)
      next.total_hint = Number(event.total_hint || 0)
      next.error = String(event.error || '')
      next.download_state = String(event.download_state || next.download_state)
    } else if (event.type === 'user_file_progress') {
      const id = String(event.aweme_id || '')
      if (next.items[id] !== 'downloading') return current
      const written = Number(event.written)
      const expected = event.expected === null ? 0 : Number(event.expected)
      const rate = Number(event.rate)
      if (!Number.isSafeInteger(written) || written < 0 || !Number.isSafeInteger(expected)
        || expected < 0 || !Number.isSafeInteger(rate) || rate < 0) return current
      next.itemFiles[id] = { name: String(event.name || ''), written, expected, rate,
        updatedAt: Date.now() }
    } else {
      const id = String(event.aweme_id || '')
      if (!id) return current
      const fields = { queued: 'queued', downloading: 'downloading', downloaded: 'success',
        failed: 'failed', skipped: 'skipped', cancelled: 'cancelled' }
      const previous = next.items[id]
      const state = event.outcome === 'skipped' ? 'skipped' : String(event.state || '')
      if (!fields[state]) return current
      if (fields[previous]) next[fields[previous]] -= 1
      if (!previous) next.itemOrder.push(id)
      next.items[id] = state
      if (state !== 'downloading') delete next.itemFiles[id]
      if (event.title || event.creator) next.itemMeta[id] = {
        title: String(event.title || ''), creator: String(event.creator || '') }
      next[fields[state]] += 1
      next.download_done = next.success + next.failed + next.cancelled
    }
    next.download_total = Math.max(next.accepted,
      Object.keys(next.items).length - next.skipped)
    return acceptUser(current, next)
  }
  function activeUser(task) {
    return !!task && (task.scan_state === 'scanning'
      || (task.kind === 'download' && task.download_state !== 'completed'
        && task.download_state !== 'failed' && task.download_state !== 'cancelled'))
  }
  function formatUser(task) {
    if (!task) return formatLanes(
      { label: '扫描', done: 0, total: null, tone: '' },
      { label: '下载', done: 0, total: 0, tone: '' })
    const scanning = task.scan_state === 'scanning'
    const scanPrefix = scanning ? '扫描作品' : task.scan_state === 'completed'
      ? '扫描完成' : task.scan_state === 'cancelled' ? '扫描已取消' : '扫描失败'
    const pending = task.download_done < task.download_total
    const bad = task.download_state === 'failed' || task.download_state === 'cancelled'
    const downloadPrefix = task.download_state === 'failed' ? '下载失败'
      : task.download_state === 'cancelled' ? '下载已取消' : '下载'
    return formatLanes({ label: scanPrefix, done: task.scanned,
      bare: task.scan_state === 'cancelled' || task.scan_state === 'failed',
      total: scanning && task.total_hint > 0 ? task.total_hint : null,
      suffix: task.kind === 'download' ? ` 可下载${task.accepted}` : '',
      tone: ['failed', 'cancelled'].includes(task.scan_state) ? 'bad' : task.scan_state === 'scanning' ? 'active' : 'ok',
      detail: task.error || (task.kind === 'download' && task.scan_state === 'completed' && pending ? '等待下载完成' : ''),
    }, { label: task.kind === 'browse' ? '下载' : downloadPrefix,
      done: task.kind === 'browse' ? 0 : task.download_done,
      total: task.kind === 'browse' ? 0 : task.download_total,
      tone: task.kind === 'browse'
        ? task.scan_state === 'scanning' ? 'active' : ['failed', 'cancelled'].includes(task.scan_state) ? 'bad' : 'ok'
        : bad || task.failed || task.cancelled ? 'bad' : pending || task.scan_state === 'scanning' ? 'active' : 'ok' })
  }
  const userApi = { base, accept: acceptUser, browse, download, active: activeUser, format: formatUser }
  if (typeof module !== 'undefined' && module.exports) module.exports.AccountTaskProgress = userApi
  root.AccountTaskProgress = userApi
})(typeof window !== 'undefined' ? window : globalThis)
