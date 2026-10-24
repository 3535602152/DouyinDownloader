/* LiveWIN-style shell: small state store, stable DOM regions, no resize redraw loop. */
const meta = {
  link: '链接下载', user: '用户作品', following: '关注主播', comments: '评论与图片',
  history: '下载记录', settings: '设置', streamer: '主播主页',
}
const modeLabels = { post: '作品', like: '喜欢', mix: '合集', music: '音乐', collect: '收藏', collectmix: '收藏的合集' }
function modeSupportsIncremental(mode) { return !['collect', 'collectmix'].includes(String(mode || '').trim()) }
function currentAccountUrl(profile) {
  return `https://www.douyin.com/user/${encodeURIComponent(profile.sec_uid)}`
}
function userRequestUrl() {
  return state.userAccount && state.userAccount.is_self
    && ['collect', 'collectmix'].includes(String(state.forms.userMode || ''))
    ? 'https://www.douyin.com/user/self'
    : state.userAccount ? currentAccountUrl(state.userAccount) : state.forms.userUrl
}
const typeLabels = { video: '视频', gallery: '图文', note: '笔记', music: '音乐' }
const streamerCursorLoopError = '作品列表加载异常，请刷新后重试'
const THEME_STORAGE_KEY = 'douyin-downloader-theme'
function readThemeCache() {
  try { const value = localStorage.getItem(THEME_STORAGE_KEY); return ['light', 'dark', 'system'].includes(value) ? value : 'light' } catch { return 'light' }
}
function cacheTheme(theme) {
  try { localStorage.setItem(THEME_STORAGE_KEY, theme) } catch {}
}
function syncEffectiveTheme() {
  const root = document.documentElement
  if (!root) return
  const dark = state.theme === 'dark' || (state.theme === 'system' && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-color-scheme: dark)').matches)
  root.dataset.theme = dark ? 'dark' : 'light'
  root.toggleAttribute('dark', dark)
  const api = window.pywebview && window.pywebview.api
  if (api && typeof api.set_titlebar_theme === 'function') {
    const result = api.set_titlebar_theme(dark ? 'dark' : 'light')
    if (result && typeof result.catch === 'function') result.catch(() => {})
  }
  syncThemeControls()
}
function applyTheme(theme) {
  const root = document.documentElement
  if (!root) return
  root.dataset.theme = theme === 'dark' ? 'dark' : 'light'
  syncEffectiveTheme()
}
function syncThemeControls() {
  document.querySelectorAll('[data-action="theme-set"]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.themeValue === state.theme))
    button.setAttribute('aria-disabled', String(state.themeSaving))
  })
  const group = document.querySelector('.sidebar-theme')
  if (group) group.setAttribute('aria-busy', String(state.themeSaving))
  const toggle = document.querySelector('.sidebar-theme-toggle')
  if (toggle) {
    toggle.setAttribute('aria-label', document.documentElement.dataset.theme === 'dark' ? '切换到浅色主题' : '切换到深色主题')
    toggle.setAttribute('aria-disabled', String(state.themeSaving))
    if (toggle.hasAttribute('aria-describedby')) showNavTip(toggle)
  }
  if (window.SettingsUI) {
    document.querySelectorAll('input[data-bind="theme"]').forEach(input => {
      input.checked = input.value === state.theme
      input.closest('.segment')?.classList.toggle('checked', input.checked)
    })
    const content = document.getElementById('content')
    if (content) syncSegmentThumbs(content)
  }
}
async function changeTheme(next) {
  if (state.themeSaving || !['light', 'dark', 'system'].includes(next) || next === state.theme) return
  const previous = state.theme
  state.themeSaving = true
  state.theme = next
  applyTheme(next)
  const saved = await call('set_theme', next)
  state.themeSaving = false
  if (saved === false || (saved === null && bridge())) {
    state.theme = previous
    applyTheme(previous)
    showToast('主题未能保存')
  } else {
    state.settings.theme = next
    if (state.forms) state.forms.theme = next
    if (state.settingsSnapshot) {
      const savedSnapshot = JSON.parse(state.settingsSnapshot); savedSnapshot.theme = next
      state.settingsSnapshot = JSON.stringify(savedSnapshot)
    }
    cacheTheme(next)
    syncThemeControls()
  }
}
window.syncEffectiveTheme = syncEffectiveTheme
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', syncEffectiveTheme)
const streamerRelatedCloseIcon = '<svg width="36" height="36" viewBox="0 0 36 36" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true"><path d="M13 13l10 10M23 13L13 23"/></svg>'
const monitorColumnDefaults = ['monitor', 'nickname', 'status', 'lastCheck', 'hiddenWorks', 'publicCount', 'localCount', 'last24h', 'storage', 'comments',
  'latestWork', 'commentsBytes', 'newWorks', 'latestPublish', 'idleDays', 'mediaMix', 'videoDuration', 'topDigg', 'avgDigg', 'commentsSaved', 'douyinId']
// Optional columns start hidden; a column added in a later version is hidden too.
const monitorColumnsHiddenByDefault = ['latestWork', 'commentsBytes', 'newWorks', 'latestPublish', 'idleDays', 'mediaMix', 'videoDuration', 'topDigg', 'avgDigg', 'commentsSaved', 'douyinId']
const MONITOR_COLUMNS_STORAGE_KEY = 'douyin-downloader-monitor-columns'
const monitorColumnSpecs = {
  monitor: { label: '监控', width: '66px' },
  nickname: { label: '主播', width: '142px' },
  status: { label: '状态', width: '100px' },
  lastCheck: { label: '上次检查', width: '128px' },
  latestWork: { label: '最新作品' },
  publicCount: { label: '主页作品', width: '96px', className: 'num', title: '抖音主页显示的作品数' },
  last24h: { label: '24h 下载', width: '92px', className: 'num', title: '最近 24 小时下载到本地的作品数' },
  localCount: { label: '本地作品', width: '96px', className: 'num', title: '已下载到本地的作品数，含作者已隐藏的作品' },
  storage: { label: '占用空间', width: '112px', className: 'num', title: '该主播本地文件的实际占用' },
  comments: { label: '评论', width: '66px', title: '开启后，每次全量扫描完成都会重新保存该主播已下载作品的评论和回复' },
  commentsBytes: { label: '评论占用', width: '104px', className: 'num', title: '该主播已保存的评论文件（*_comments.json）的实际占用' },
  hiddenWorks: { label: '已隐藏作品', width: '104px', className: 'num', title: '已登记、但全量扫描时主页已看不到（隐藏、私密或删除）的作品数' },
  newWorks: { label: '新增', width: '100px', className: 'num', title: '最近一段时间内发布的作品数' },
  latestPublish: { label: '最新发布', width: '128px', title: '最新一条作品的发布时间' },
  idleDays: { label: '断更天数', width: '96px', className: 'num', title: '距最新一条作品发布过去的天数' },
  mediaMix: { label: '视频/图文', width: '104px', className: 'num', title: '已登记的视频作品数 / 图文作品数' },
  videoDuration: { label: '总时长', width: '104px', className: 'num', title: '已下载视频的总时长' },
  topDigg: { label: '最高点赞', width: '100px', className: 'num', title: '点赞最多的作品（扫描时的数据）' },
  avgDigg: { label: '平均点赞', width: '100px', className: 'num', title: '每条作品的平均点赞数（扫描时的数据）' },
  commentsSaved: { label: '评论已存', width: '104px', className: 'num', title: '已完整保存评论的作品数 / 已下载作品数' },
  douyinId: { label: '抖音号', width: '132px', title: '主播的抖音号' },
}
function readMonitorColumnPrefs() {
  const prefs = { columns: monitorColumnDefaults.slice(), hidden: new Set(monitorColumnsHiddenByDefault), newWindow: 1 }
  let saved = null
  try { saved = JSON.parse(localStorage.getItem(MONITOR_COLUMNS_STORAGE_KEY) || 'null') } catch {}
  if (!saved || typeof saved !== 'object') return prefs
  // Upgrade an untouched previous default, but keep custom ordering/visibility.
  const oldColumns = ['monitor', 'nickname', 'status', 'lastCheck', 'latestWork', 'publicCount', 'last24h', 'localCount', 'storage', 'comments', 'commentsBytes',
    'hiddenWorks', 'newWorks', 'latestPublish', 'idleDays', 'mediaMix', 'videoDuration', 'topDigg', 'avgDigg', 'commentsSaved', 'douyinId']
  const oldHidden = ['hiddenWorks', 'newWorks', 'latestPublish', 'idleDays', 'mediaMix', 'videoDuration', 'topDigg', 'avgDigg', 'commentsSaved', 'douyinId']
  if (Array.isArray(saved.columns) && saved.columns.length === oldColumns.length &&
      saved.columns.every((key, index) => key === oldColumns[index]) &&
      Array.isArray(saved.hidden) && saved.hidden.length === oldHidden.length &&
      oldHidden.every(key => saved.hidden.includes(key))) {
    return { ...prefs, newWindow: Number(saved.newWindow) === 7 ? 7 : 1 }
  }
  const known = Array.isArray(saved.columns) ? [...new Set(saved.columns.filter(key => monitorColumnSpecs[key]))] : []
  const columns = known.slice()
  monitorColumnDefaults.forEach(key => { if (!columns.includes(key)) columns.push(key) })
  const hidden = new Set(Array.isArray(saved.hidden) ? saved.hidden.filter(key => monitorColumnSpecs[key]) : [])
  monitorColumnsHiddenByDefault.forEach(key => { if (!known.includes(key)) hidden.add(key) })
  if (columns.every(key => hidden.has(key))) hidden.delete('nickname')
  return { columns, hidden, newWindow: Number(saved.newWindow) === 7 ? 7 : 1 }
}
function saveMonitorColumnPrefs() {
  try {
    localStorage.setItem(MONITOR_COLUMNS_STORAGE_KEY, JSON.stringify({
      columns: state.monitorColumns, hidden: [...state.hiddenMonitorColumns], newWindow: state.monitorNewWindow,
    }))
  } catch {}
}
function monitorColumnLabel(key) {
  if (key === 'newWorks') return `新增（${state.monitorNewWindow === 7 ? '7天' : '1天'}）`
  return monitorColumnSpecs[key] ? monitorColumnSpecs[key].label : ''
}
const monitorColumnPrefs = readMonitorColumnPrefs()
const state = {
  startup: window.chrome?.webview ? 'settings' : '',
  page: 'link', theme: readThemeCache(), themeSaving: false, settings: {}, monitors: [], history: { items: [], total: 0, page: 1, size: 50 },
  errors: { monitor: '', history: '' }, dialogFocusReturn: null, imagePreviewReturnFocus: null,
  linkItems: [], userItems: [], comments: [], images: [], logs: [], downloadStatus: '', activities: {}, scanLine: { text: '', detail: '', running: false, countdown: 0, at: 0 }, pageDownloads: {}, dotPhase: 1,
  busy: { link: false, user: false, comments: false, localValidate: false }, taskCounts: { link: 0, user: 0, comments: 0, monitor: 0, settings: 0 }, localValidate: { busy: false, scanned: 0, total: 0 }, progress: null, logOpen: false, logTab: 'logs', riskReport: { status: 'idle', report: null, error: '' },
  userAccount: null, userAccountLoading: false, qrLogin: null,
  subscriptions: [], subscriptionMenuId: '', subscriptionDelete: null,
  subscriptionAdd: { open: false, source: '', url: '', like: true, collect: true, busy: false, error: '' },
  userTaskProgress: { browse: null, download: null, starting: null },
  monitorColumns: monitorColumnPrefs.columns, hiddenMonitorColumns: monitorColumnPrefs.hidden, monitorNewWindow: monitorColumnPrefs.newWindow, columnPickerOpen: false,
  monitorMenuId: '', monitorHoverId: '', monitorDelete: null, pendingMonitorDeletes: new Set(), monitorAddOpen: false, monitorAddSubmitting: false, monitorAddSubmittedUrls: [], monitorAddFailedUrlsDuringSubmit: new Set(),
  followingImport: {
    open: false, phase: 'idle', requestId: '', loaded: 0, pages: 0, totalHint: 0,
    account: {}, items: [], selected: new Set(), page: 1, error: '',
  },
  streamer: {
    secUid: '', creator: {}, works: [], source: 'local', hasMore: false,
    loading: false, loadingMore: false, cursor: 0, offset: 0, mode: 'local',
    profileKnown: false, remoteCursors: new Set(), paginationStopped: false,
    error: '', profileError: '', returnPage: 'following', returnScroll: 0, tab: 'works', subtab: 'works', autoPlay: false, clearScreen: false, volume: 1,
    search: '', date: '', modal: null,
  },
  selected: { link: new Set(), user: new Set(), images: new Set() },
  forms: { links: '', userUrl: '', userMode: 'like', userLimit: 0, userIncrease: true,
    ...SettingsUI.defaults(), cover: true, music: true, avatar: true, json: true,
    startTime: '', endTime: '', increase: true, homepageScreenshot: true,
    cookie: '', cookieOpen: false, clearCookie: false, commentSource: '',
    historyAuthor: '', historyTitle: '', historyType: '' },
  settingsSnapshot: '',
}
state.forms.theme = state.theme
applyTheme(state.theme)
let toastTimer = null
let riskRefreshTimer = null
let riskReportRequest = 0
let pollInFlight = false
let pollingStarted = false
let bootstrapRunning = false
let historyRequestGeneration = 0
let streamerWheelLock = false
let streamerPreloadGeneration = 0
const streamerPreloadVideos = new Map()
const streamerDetailRequests = new WeakMap()
let streamerProgressDrag = null
let streamerVolumeDrag = null
let draggingMonitorColumn = null
let draggingMonitorRow = null
let draggingMonitorOrder = null
let draggingMonitorPointerId = null
let draggingMonitorPlaceholder = null
let draggingMonitorRowStyle = ''
let draggingMonitorOffsetX = 0
let draggingMonitorOffsetY = 0
const pendingMonitorEnabled = {}
const pendingMonitorComments = {}
const byId = id => document.getElementById(id)
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]))
const bridge = () => window.pywebview && window.pywebview.api
async function call(name, ...args) {
  const target = bridge()
  // No bridge yet means the page is still booting — a normal state, not an
  // error.  A *missing method* is a wiring bug, and returning a silent null for
  // it turns a typo into a button that looks like it does nothing.
  if (!target) return null
  if (typeof target[name] !== 'function') {
    console.error('[bridge] 桥接方法不存在：' + name + '()')
    showToast('功能不可用：缺少桥接方法 ' + name)
    return null
  }
  try { return await target[name](...args) } catch (error) { showToast(error instanceof Error ? error.message : String(error)); return null }
}
window.addEventListener('pywebviewready', () => applyTheme(state.theme), { once: true })

// ── custom window caption (titlebar.css, gui/window_frame.py) ──
const titlebarHeight = () => document.querySelector('.titlebar')?.offsetHeight || 0
async function syncWindowFrame() {
  const target = bridge()
  if (!target || typeof target.get_window_frame !== 'function') return
  const frame = await target.get_window_frame().catch(() => null)
  if (!frame) return
  if (!frame.custom) { delete document.documentElement.dataset.frame; return }
  document.documentElement.toggleAttribute('data-window-maximized', !!frame.maximized)
  const button = document.querySelector('.titlebar [data-window="maximize"]')
  if (!button) return
  const label = frame.maximized ? '还原' : '最大化'
  button.textContent = frame.maximized ? '' : ''
  button.title = label
  button.setAttribute('aria-label', label)
}
window.addEventListener('pywebviewready', syncWindowFrame, { once: true })
window.addEventListener('resize', () => { if (document.documentElement.dataset.frame) syncWindowFrame() })
document.addEventListener('click', event => {
  const button = event.target.closest?.('.titlebar [data-window]')
  if (button) call('window_command', button.dataset.window)
})
// The WebView reaches the window's top edge, so the page owns the top resize
// border and hands the press to Windows while the button is still down.
document.addEventListener('mousedown', event => {
  const edge = event.button === 0 && event.target.closest?.('.titlebar [data-edge]')
  if (!edge) return
  event.preventDefault()
  call('window_start_resize', edge.dataset.edge)
})
function showToast(text) {
  if (!text) return
  const node = byId('toast')
  // The reporter for a failed action must never throw itself.
  if (!node) return
  node.textContent = text; node.classList.add('show')
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => node.classList.remove('show'), 3600)
}
function reportActionError(error, event) {
  const node = event && event.target && event.target.closest ? event.target.closest('[data-action]') : null
  const action = node && node.dataset ? node.dataset.action : ''
  const message = error instanceof Error ? error.message : String(error)
  console.error('[action] ' + (action || 'click') + ' 处理失败：', error)
  showToast('操作失败：' + message)
}
function formatMonitorTime(value) {
  const timestamp = Number(value)
  if (!Number.isFinite(timestamp) || timestamp <= 0) return '—'
  // Relative ages only: 刚刚 / 3分钟前 / 5小时26分钟前 / 2天5小时26分钟前.
  // Day counts are kept unbounded so even multi-year gaps stay readable
  // (1453天5小时26分钟前) instead of becoming an absolute date again.
  const seconds = Math.floor(Date.now() / 1000 - timestamp)
  if (seconds < 60) return '刚刚'
  const minutes = Math.floor(seconds / 60)
  const hours = Math.floor(minutes / 60)
  const days = Math.floor(hours / 24)
  const parts = []
  if (days > 0) parts.push(`${days}天`)
  if (hours % 24 > 0) parts.push(`${hours % 24}小时`)
  if (minutes % 60 > 0) parts.push(`${minutes % 60}分钟`)
  return `${parts.join('')}前`
}
function formatMonitorStorageBytes(value) {
  const bytes = Number(value)
  const megabytes = Number.isFinite(bytes) ? Math.max(0, bytes) / (1024 * 1024) : 0
  return `${megabytes.toLocaleString('en-US', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
    useGrouping: true,
  })} MB`
}
function formatMonitorBytes(value) { return TaskPresentation.bytes(value) }
function formatMonitorCount(value) { return TaskPresentation.count(value) }
function formatMonitorDuration(ms) {
  const value = Math.max(0, Number(ms) || 0)
  const minutes = Math.round(value / 60000)
  if (minutes <= 0) return value > 0 ? '<1分钟' : '—'
  const hours = Math.floor(minutes / 60)
  if (!hours) return `${minutes}分钟`
  return minutes % 60 ? `${hours}小时${minutes % 60}分` : `${hours}小时`
}
function monitorIdleDays(timestamp) {
  const value = Number(timestamp)
  if (!Number.isFinite(value) || value <= 0) return null
  return Math.max(0, Math.floor((Date.now() / 1000 - value) / 86400))
}
function tickMonitorTimes() {
  if (state.page !== 'following' && state.page !== 'user') return
  document.querySelectorAll('#content td[data-ts]').forEach(node => {
    const next = formatMonitorTime(node.dataset.ts)
    if (node.textContent !== next) node.textContent = next
  })
  document.querySelectorAll('#content td[data-due]').forEach(node => {
    const next = formatScanDue(node.dataset.due)
    if (node.textContent !== '—' && node.textContent !== next) node.textContent = next
  })
}
function tickFullScanSchedule() {
  if (state.page !== 'following') return
  const scanButton = document.querySelector('#content [data-action="monitor-full-scan"]')
  if (scanButton) scanButton.title = fullScanScheduleTitle()
}
function alignMonitorSwitches() {
  const dpr = Number(window.devicePixelRatio) || 1
  document.querySelectorAll('#content .switch').forEach(node => {
    // Reset first so repeated calls (resize / DPI change) measure layout, not
    // the correction from the previous pass.
    node.style.setProperty('--switch-y-snap', '0px')
    const top = node.getBoundingClientRect().top
    const correction = (Math.round(top * dpr) - top * dpr) / dpr
    node.style.setProperty('--switch-y-snap', `${correction}px`)
  })
}
function setForm(name, value) { state.forms[name] = value }
function button(label, action, variant = 'secondary', extra = '', busy = false) {
  return `<button type="button" class="button ${variant}${busy ? ' is-busy' : ''}" data-action="${action}" ${busy ? 'aria-busy="true"' : ''} ${extra}>${label}</button>`
}
function progressButton(label, action, progress, busy) {
  const value = Math.max(0, Math.min(100, Number(progress) || 0))
  return `<button type="button" class="button secondary progress-button${busy ? ' is-busy' : ''}" data-action="${action}" style="--progress:${value}%"${busy ? ' aria-busy="true" disabled' : ''}><span>${label}</span></button>`
}
function panel(title, body, actions = '') {
  return `<section class="panel"><div class="panel-head"><h2>${title}</h2></div><div class="panel-body">${body}</div>${actions}</section>`
}
function actionBar(content) { return `<div class="action-bar">${content}</div>` }
const inputAccessibleNames = {
  path: '下载目录',
  retry: '失败重试次数',
  userLimit: '数量',
  filenameTemplate: '文件名模板',
  folderTemplate: '文件夹模板',
}
function input(name, value, placeholder = '', cls = '', disabled = false) {
  const label = inputAccessibleNames[name] || placeholder || name
  return `<input class="${cls}" data-bind="${name}" value="${esc(value)}" placeholder="${esc(placeholder)}" aria-label="${esc(label)}"${disabled ? ' disabled' : ''}>`
}
const emptyIcon = '<svg class="empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>'
function emptyState(title, hint = '') {
  return `<div class="empty">${emptyIcon}<strong>${esc(title)}</strong>${hint ? `<span>${esc(hint)}</span>` : ''}</div>`
}
function selected(scope, id) { return state.selected[scope].has(String(id)) }
function check(scope, id) {
  const on = selected(scope, id)
  const item = scope === 'images' ? '图片' : '作品'
  return `<button type="button" class="check${on ? ' is-checked' : ''}" data-action="toggle-check" data-scope="${scope}" data-id="${esc(id)}" aria-label="${on ? '取消选择' : '选择'}${item}" aria-pressed="${on}"></button>`
}
function extractCommentAwemeId(value) {
  const raw = String(value ?? '').trim()
  if (/^\d+$/.test(raw)) return raw

  // A Douyin modal opened from a profile/discover/search page uses
  // `?modal_id=<aweme_id>` instead of a `/video/` or `/note/` path.
  const modal = raw.match(/[?&]modal_id=(\d+)/i)
  if (modal) return modal[1]

  const path = raw.match(/\/(?:video|note|gallery|slides)\/(\d+)/i)
  if (path) return path[1]

  // A few profile URLs only expose the work as `vid`; keep it as a
  // fallback after the canonical modal/path forms.
  const vid = raw.match(/[?&](?:vid|aweme_id|item_id)=(\d+)/i)
  if (vid) return vid[1]

  // Share sheets commonly paste a sentence around a v.douyin.com short URL.
  // Return that URL to Python, where it is resolved asynchronously.
  const shared = raw.match(/https?:\/\/[^\s\u3000，。；！？、（）【】《》“”‘’]+/i)
  if (shared) {
    return shared[0].replace(/[.,;:!?)}\]}>，。；！？、）】》]+$/g, '')
  }
  if (/^v\.douyin\.com\//i.test(raw)) return raw
  return raw
}
function statusBadge(value) {
  const status = TaskPresentation.describe(value)
  return `<span class="status-badge ${status.tone}" title="${esc(status.label)}">${esc(status.label)}</span>`
}
function syncItemStatus(scope, id) {
  const items = scope === 'user' ? state.userItems : state.linkItems
  const work = items.find(item => String(item.aweme_id) === String(id))
  const row = document.querySelector(`[data-work-scope="${CSS.escape(scope)}"][data-work-id="${CSS.escape(String(id))}"]`)
  if (row && work) row.lastElementChild.innerHTML = statusBadge(work.status)
  WorkDetails.update()
}
function itemRows(items, scope, busy) {
  if (!items.length) return emptyState(busy ? '正在解析…' : '暂无作品', busy ? '' : '在上方粘贴链接并点「解析」，作品会列在这里')
  const allOn = items.every(item => selected(scope, item.aweme_id))
  const selectAll = `<button type="button" class="check${allOn ? ' is-checked' : ''}" data-action="select-all" data-scope="${scope}" aria-label="全选" aria-pressed="${allOn}"${busy ? ' disabled' : ''}></button>`
  return `<div class="table-wrap"><table><thead><tr><th style="width:56px">${selectAll}</th><th>标题</th><th style="width:120px">合集</th><th style="width:160px">发布时间</th><th style="width:80px">时长</th><th style="width:74px">类型</th><th style="width:100px">状态</th></tr></thead><tbody>${items.map(item => { const isSelected = selected(scope, item.aweme_id); return `<tr data-work-scope="${scope}" data-work-id="${esc(item.aweme_id)}" class="${isSelected ? 'selected' : ''}" aria-selected="${isSelected}"><td>${check(scope, item.aweme_id)}</td><td class="title" title="${esc(item.title)}"><button type="button" class="work-detail-trigger" data-action="work-details-open" data-scope="${scope}" data-id="${esc(item.aweme_id)}">${esc(item.title || '无标题')}</button></td><td title="${esc(item.collection_name || '—')}">${esc(item.collection_name || '—')}</td><td title="${esc(item.publish_time || '—')}">${esc(item.publish_time || '—')}</td><td>${esc(item.duration || '—')}</td><td>${esc(item.media_type || '—')}</td><td>${statusBadge(item.status)}</td></tr>` }).join('')}</tbody></table></div>`
}
function bodyPanel(body) { return `<section class="panel"><div class="panel-body">${body}</div></section>` }
function taskControls(busy) {
  return busy ? `${button('暂停', 'pause', 'secondary')}${button('继续', 'resume', 'secondary')}${button('终止', 'stop', 'danger')}` : ''
}
function renderLink() {
  const busy = state.busy.link
  const items = state.linkItems
  const picked = state.selected.link.size
  const body = `<textarea data-bind="links" aria-label="抖音链接" placeholder="粘贴抖音链接，每行一个"${busy ? ' disabled' : ''}>${esc(state.forms.links)}</textarea><div class="toolbar link-entry-actions" style="margin-top:12px"><span class="spacer"></span>${button(busy ? '处理中…' : '解析', 'link-preview', 'primary', busy ? 'disabled' : '', busy)}</div>`
  const table = `<div class="panel">${itemRows(items, 'link', busy)}${!items.length && !busy ? '' : actionBar(`<span class="label">${items.length ? `已选 ${picked} / ${items.length}` : ''}</span><span class="spacer"></span>${taskControls(busy)}${button('下载所选', 'link-download', 'primary', busy || !picked ? 'disabled' : '')}`)}</div>`
  return `<div class="stack">${bodyPanel(body)}${table}</div>`
}
function formatScanDue(value) {
  const timestamp = Number(value)
  if (!Number.isFinite(timestamp) || timestamp <= 0) return '随时'
  const seconds = Math.floor(timestamp - Date.now() / 1000)
  if (seconds <= 60) return '随时'
  // One rounded unit keeps the column narrow: 40分钟后 / 4小时后 / 2天后.
  const minutes = Math.ceil(seconds / 60)
  if (minutes < 60) return `${minutes}分钟后`
  const hours = Math.round(minutes / 60)
  return hours < 48 ? `${hours}小时后` : `${Math.round(hours / 24)}天后`
}
function subscriptionStatus(row) {
  const counts = row.counts || {}
  const scan = row.scanning
  if (scan) {
    const kind = scan.mode === 'full' ? '全量扫描' : '增量扫描'
    return { text: `${kind}中`, tone: 'info', title: `第 ${Number(scan.pages || 0)} 页 · 已读 ${Number(scan.seen || 0)} · 新增 ${Number(scan.queued || 0)}` }
  }
  if (row.enabled && row.blocked) return { text: /失效/.test(row.blocked) ? '登录失效' : '无法扫描', tone: 'muted', title: `${row.blocked}；扫描已跳过` }
  if (row.requested) return { text: '排队扫描', tone: 'info', title: row.requested === 'full' ? '全量扫描' : '增量扫描' }
  const error = String(row.last_error || '')
  if (row.paused && /风控/.test(error)) return { text: '风控暂停', tone: 'warn', title: `${error} · ${formatScanDue(row.paused_until)}继续` }
  if (row.paused && error) return { text: '异常', tone: 'error', title: `${error} · ${formatScanDue(row.paused_until)}重试` }
  if (!row.enabled) return { text: '已暂停', tone: 'muted', title: '' }
  if (Number(row.full_epoch || 0) > 0) return { text: '全量扫描未完成', tone: 'info', title: `已读 ${Number(row.full_seen || 0)}，下次从第 ${Number(row.full_pages || 0) + 1} 页继续` }
  if (Number(counts.pending || 0) > 0) return { text: '待下载', tone: 'info', title: `${Number(counts.pending)} 个作品排队（关注主播优先）` }
  if (Number(row.last_full_at || 0) <= 0) return { text: '等待首次扫描', tone: 'muted', title: '' }
  return { text: '正常', tone: 'ok', title: String(row.last_note || '') }
}
function subscriptionAccountCell(row) {
  const name = String(row.nickname || row.account_sec_uid || '抖音用户')
  // A library account's avatar is cached with its login even when the row has none.
  const library = row.is_self ? libraryAccounts().find(item => String(item.sec_uid) === String(row.account_sec_uid)) : null
  const src = row.avatar_url || (library && library.avatar_data) || ''
  const avatar = src
    ? `<img class="following-avatar" src="${esc(src)}" alt="">`
    : `<span class="following-avatar following-avatar-fallback">${esc(name.slice(0, 1))}</span>`
  const label = `<button type="button" class="creator-link" title="${esc(name)}" aria-label="打开主页：${esc(name)}" data-action="subscription-browse" data-id="${esc(String(row.id))}">${esc(name)}</button>`
  return `<td class="subscription-account"><div class="following-person">${avatar}<div class="following-person-copy">${label}<span>${esc(row.kind_label || row.kind)}${row.is_self ? ' · 账号库' : ' · 用主账号读取'}</span></div></div></td>`
}
function subscriptionRows() {
  const rows = state.subscriptions
  if (!rows.length) return emptyState('还没有订阅的账号', '添加账号后，会定时扫描它的喜欢或收藏，把新作品下载到「喜欢 / 收藏」文件夹。')
  const head = '<tr><th style="width:66px">订阅</th><th>账号</th><th class="num" title="已下载（含本地其他目录已有的）/ 上次全量扫描时云端数量">已下载/云端</th><th class="num">待下载</th><th class="num">失败</th><th class="num" title="从列表里消失的作品：确认取消的已删除本地文件，无法确认的只标记">已移出</th><th title="悬停查看上次扫描时间">下次扫描</th><th>状态</th><th style="width:56px"></th></tr>'
  const body = rows.map(row => {
    const id = String(row.id)
    const counts = row.counts || {}
    const status = subscriptionStatus(row)
    const lastScan = Math.max(Number(row.last_full_at || 0), Number(row.last_incremental_at || 0))
    const open = state.subscriptionMenuId === id
    const title = status.title ? ` title="${esc(status.title)}"` : ''
    const gone = Number(counts.missing || 0) + Number(counts.removed || 0)
    const cloud = Number(row.cloud_count || 0)
    return `<tr data-subscription-id="${esc(id)}"><td class="switch-cell"><button type="button" class="switch${row.enabled ? ' on' : ''}" data-action="subscription-toggle" data-id="${esc(id)}" aria-pressed="${!!row.enabled}" aria-label="订阅开关"><i></i></button></td>${subscriptionAccountCell(row)}<td class="num">${Number(counts.downloaded || 0)} / ${cloud || '—'}</td><td class="num">${Number(counts.pending || 0)}</td><td class="num">${Number(counts.failed || 0)}</td><td class="num" title="已删除 ${Number(counts.removed || 0)} · 仅标记 ${Number(counts.missing || 0)}">${gone}</td><td data-due="${Number(row.next_scan_at || 0)}" title="上次扫描：${esc(formatMonitorTime(lastScan))}">${row.enabled ? formatScanDue(row.next_scan_at) : '—'}</td><td class="status-cell"><span class="status-badge ${status.tone}"${title}><span class="status-text">${esc(status.text)}</span></span></td><td class="monitor-actions-cell"><button type="button" class="icon-button monitor-row-menu-trigger" data-action="subscription-menu-toggle" data-id="${esc(id)}" aria-label="订阅操作" aria-haspopup="menu" aria-expanded="${open}">⋯</button></td></tr>`
  }).join('')
  return `<div class="table-wrap" data-ui-scroll-key="subscription-table"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>`
}
function subscriptionRowMenu() {
  const id = state.subscriptionMenuId
  const row = state.subscriptions.find(item => String(item.id) === id)
  if (!id || !row) return ''
  const browse = `<button type="button" role="menuitem" data-action="subscription-browse" data-id="${esc(id)}">打开主页</button>`
  const retry = Number((row.counts || {}).failed || 0) > 0
    ? `<button type="button" role="menuitem" data-action="subscription-retry" data-id="${esc(id)}">重试失败作品</button>`
    : ''
  return `<div id="subscription-row-menu" class="monitor-row-menu" role="menu" data-subscription-menu><button type="button" role="menuitem" data-action="subscription-scan" data-id="${esc(id)}">检查更新</button><button type="button" role="menuitem" data-action="subscription-full-scan" data-id="${esc(id)}">全量扫描</button>${browse}${retry}<div class="monitor-row-menu-divider" aria-hidden="true"></div><button type="button" class="danger-text" role="menuitem" data-action="subscription-delete" data-id="${esc(id)}">删除订阅</button></div>`
}
function alignSubscriptionMenu() {
  const menu = byId('subscription-row-menu')
  const trigger = [...document.querySelectorAll('[data-action="subscription-menu-toggle"]')]
    .find(node => node.dataset.id === state.subscriptionMenuId)
  if (!menu || !trigger) return
  const rect = trigger.getBoundingClientRect()
  const bounds = menu.getBoundingClientRect()
  AppUI.rememberMenuAnchor(menu, trigger)
  const top = rect.bottom + bounds.height + 8 <= window.innerHeight ? rect.bottom + 4 : Math.max(8, rect.top - bounds.height - 4)
  menu.style.top = `${top}px`
  menu.style.left = `${Math.max(8, Math.min(window.innerWidth - bounds.width - 8, rect.right - bounds.width))}px`
}
function closeSubscriptionMenu() {
  if (!state.subscriptionMenuId) return ''
  const id = state.subscriptionMenuId
  state.subscriptionMenuId = ''
  byId('subscription-row-menu')?.remove()
  document.querySelectorAll('[data-action="subscription-menu-toggle"]').forEach(node => node.setAttribute('aria-expanded', 'false'))
  return id
}
function subscriptionAddModal() {
  const form = state.subscriptionAdd
  if (!form.open) return ''
  const busy = !!form.busy
  const disabled = busy ? ' disabled' : ''
  const accounts = libraryAccounts()
  const byLink = form.source === 'url' || !accounts.length
  const other = byLink && !!String(form.url || '').trim()
  const sources = accounts.length
    ? `<select class="account-select grow" data-bind="subscriptionSource" aria-label="订阅哪个账号"${disabled}>${accounts.map(item => { const value = item.is_main ? '' : String(item.sec_uid); return `<option value="${esc(value)}"${!byLink && value === String(form.source || '') ? ' selected' : ''}>${esc(item.nickname || '抖音用户')}${item.is_main ? '（主账号）' : ''}</option>` }).join('')}<option value="url"${byLink ? ' selected' : ''}>别人的主页链接…</option></select>`
    : ''
  const link = byLink ? input('subscriptionUrl', form.url, accounts.length ? '用户主页链接' : '用户主页链接（留空 = 我的账号）', 'grow', busy) : ''
  return `<div class="following-overlay monitor-add-overlay" data-subscription-add-overlay role="dialog" aria-modal="true" aria-labelledby="subscription-add-title"><section class="following-dialog monitor-add-dialog" tabindex="-1"><div class="following-dialog-head"><div><h2 id="subscription-add-title">添加订阅账号</h2><p>账号库里的账号用它自己的登录扫描喜欢和收藏；别人的喜欢用主账号读取，需要对方公开喜欢列表。</p></div><button type="button" class="icon-button" data-ui-dismiss data-action="subscription-add-close" aria-label="关闭"${disabled}>×</button></div><div class="monitor-add-body subscription-add-body">${sources}${link}<label class="check-line"><input type="checkbox" data-bind="subscriptionLike"${form.like ? ' checked' : ''}${disabled}>喜欢</label><label class="check-line"${other ? ' title="收藏只能订阅账号库里的账号"' : ''}><input type="checkbox" data-bind="subscriptionCollect"${form.collect && !other ? ' checked' : ''}${busy || other ? ' disabled' : ''}>收藏（仅账号库里的账号）</label>${form.error ? `<p class="monitor-delete-error" role="alert">${esc(form.error)}</p>` : ''}</div><div class="following-dialog-actions"><span class="spacer"></span><button type="button" class="button secondary" data-ui-dismiss data-action="subscription-add-close"${disabled}>取消</button>${button(busy ? '正在读取账号…' : '添加', 'subscription-add', 'primary', busy ? 'disabled' : '', busy)}</div></section></div>`
}
function subscriptionDeleteDialog() {
  const request = state.subscriptionDelete
  if (!request) return ''
  return `<div class="monitor-delete-overlay" data-subscription-delete-overlay><section class="monitor-delete-dialog" role="dialog" aria-modal="true" aria-labelledby="subscription-delete-title"><div class="monitor-delete-head"><h2 id="subscription-delete-title">删除订阅</h2><button type="button" class="icon-button" data-ui-dismiss data-action="subscription-delete-cancel" aria-label="关闭">×</button></div><p>不再扫描「${esc(request.name)}」？已下载的文件会保留在本地。</p><div class="monitor-delete-actions"><button type="button" class="button secondary" data-ui-dismiss data-action="subscription-delete-cancel">取消</button><button type="button" class="button danger" data-action="subscription-delete-confirm">删除订阅</button></div></section></div>`
}
function renderUser() {
  const enabled = state.subscriptions.filter(row => row.enabled)
  const note = '增量每 6 小时，遇到已入库作品即停；全量每 3 天，翻页节奏和关注主播一样；触发风控暂停 12 小时后从断点继续。账号库里的账号用各自的登录轮流扫描，其他人的喜欢用主账号读取。关注主播有扫描任务时先让主播扫描，下载也在关注主播队列空闲时进行。'
  // A resumed pre-subscription download still runs here; keep its controls.
  const legacy = state.busy.user ? `<span class="label">正在继续上次的用户下载</span>${taskControls(true)}` : ''
  return `<div class="stack"><div class="monitor-entry-actions">${button('添加账号', 'subscription-add-open', 'primary')}<span class="spacer"></span>${legacy}</div><div class="panel monitor-panel subscription-panel">${subscriptionRows()}${subscriptionRowMenu()}${actionBar(`<span class="label" title="${esc(note)}">增量 6 小时 · 全量 3 天慢速 · 风控暂停 12 小时</span><span class="spacer"></span>${button('全量扫描', 'subscription-full-scan-all', 'secondary', enabled.length ? '' : 'disabled')}${button('检查更新', 'subscription-scan-all', 'secondary', enabled.length ? '' : 'disabled')}`)}</div></div>${subscriptionAddModal()}${subscriptionDeleteDialog()}`
}
async function refreshSubscriptions() {
  const rows = await call('subscriptions')
  if (Array.isArray(rows)) state.subscriptions = rows
  if (state.page === 'user') renderPage()
}
async function addSubscription() {
  const form = state.subscriptionAdd
  if (form.busy) return
  const byLink = form.source === 'url' || !libraryAccounts().length
  const url = byLink ? String(form.url || '').trim() : ''
  if (byLink && !url && libraryAccounts().length) { form.error = '请输入用户主页链接'; renderPage(); return }
  const kinds = [form.like && 'like', !url && form.collect && 'collect'].filter(Boolean)
  if (!kinds.length) { form.error = '请至少选择喜欢或收藏'; renderPage(); return }
  form.busy = true; form.error = ''; renderPage()
  let result = null
  try {
    const account = byLink ? '' : (String(form.source || '') || state.settings.accounts.find(item => item.is_main)?.sec_uid || '')
    result = await bridge().add_subscription(url || null, kinds, account)
  } catch (error) {
    form.error = String((error && error.message) || error || '添加失败')
  }
  form.busy = false
  if (result && Array.isArray(result.ids)) {
    form.open = false
    showToast(`已添加：${result.nickname || '账号'}，稍后开始首次全量扫描`)
    await refreshSubscriptions()
    return
  }
  if (!form.error) form.error = '添加失败，请检查链接或登录状态'
  renderPage()
}
async function handleSubscriptionAction(action, node) {
  const id = node.dataset.id
  const row = state.subscriptions.find(item => String(item.id) === String(id))
  if (action === 'subscription-add-open') {
    state.subscriptionAdd = { open: true, source: '', url: '', like: true, collect: true, busy: false, error: '' }
    renderPage(); return true
  }
  if (action === 'subscription-add-close') {
    if (state.subscriptionAdd.busy) return true
    state.subscriptionAdd.open = false
    renderPage(); return true
  }
  if (action === 'subscription-add') { await addSubscription(); return true }
  if (action === 'subscription-toggle' && row) {
    const accepted = await call('set_subscription_enabled', [row.id], !row.enabled)
    if (accepted === true) row.enabled = row.enabled ? 0 : 1
    renderPage(); return true
  }
  if (action === 'subscription-menu-toggle') {
    const opening = state.subscriptionMenuId !== String(id)
    closeSubscriptionMenu()
    if (opening) state.subscriptionMenuId = String(id)
    AppUI.setPageMenu(subscriptionRowMenu())
    alignSubscriptionMenu()
    document.querySelectorAll('[data-action="subscription-menu-toggle"]').forEach(button => button.setAttribute('aria-expanded', String(opening && button.dataset.id === String(id))))
    if (opening) byId('subscription-row-menu')?.querySelector('[role="menuitem"]')?.focus()
    return true
  }
  if (['subscription-scan', 'subscription-full-scan', 'subscription-scan-all', 'subscription-full-scan-all'].includes(action)) {
    const full = action.includes('full')
    const ids = action.endsWith('-all') ? state.subscriptions.filter(item => item.enabled).map(item => item.id) : row ? [row.id] : []
    closeSubscriptionMenu()
    if (!ids.length) { renderPage(); return true }
    const accepted = await call('scan_subscriptions', ids, full)
    showToast(accepted === true ? (full ? '已排队全量扫描' : '已排队检查更新') : '扫描未能排队')
    await refreshSubscriptions(); return true
  }
  if (action === 'subscription-retry' && row) {
    closeSubscriptionMenu()
    const count = await call('retry_subscriptions', [row.id])
    showToast(Number(count) > 0 ? `已重新排队 ${Number(count)} 个作品` : '没有可重试的作品')
    await refreshSubscriptions(); return true
  }
  if (action === 'subscription-browse' && row) {
    closeSubscriptionMenu(); renderPage()
    if (state.busy.user || state.userAccountLoading) { showToast('账号内容正在下载，请稍后再浏览'); return true }
    const current = state.userAccount
    const viewer = row.is_self ? String(row.account_sec_uid) : ''
    if (!current || String(current.sec_uid) !== String(row.account_sec_uid) || String(current.viewer || '') !== viewer) {
      const resolved = await resolveUserAccount(viewer ? undefined : currentAccountUrl({ sec_uid: row.account_sec_uid }), 'like', viewer)
      if (!resolved) return true
    }
    // A library account opens its own homepage (作品 / 喜欢 / 收藏), read with
    // its own login, on the tab of this subscription; anyone else's opens
    // their public homepage.
    await openAccountHome('like')
    if (row.kind === 'collect' && state.page === 'streamer' && state.streamer.catalogue) {
      state.streamer.homeSection = 'favorite_collection'
      renderPage()
    }
    return true
  }
  if (action === 'subscription-delete' && row) {
    closeSubscriptionMenu()
    state.subscriptionDelete = { id: row.id, name: `${row.nickname || '账号'}的${row.kind_label || ''}` }
    renderPage(); return true
  }
  if (action === 'subscription-delete-cancel') { state.subscriptionDelete = null; renderPage(); return true }
  if (action === 'subscription-delete-confirm') {
    const request = state.subscriptionDelete
    state.subscriptionDelete = null
    if (request) {
      const accepted = await call('delete_subscriptions', [request.id])
      showToast(accepted === true ? '已删除订阅，本地文件已保留' : '删除失败')
    }
    await refreshSubscriptions(); return true
  }
  return false
}
const accountDownloadStates = new Map()
let accountHomeGeneration = 0
let accountHomeSession = null
const accountClientId = typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : `web-${Date.now()}`
function cancelCurrentAccountPreview() {
  const catalogue = accountHomeSession && accountHomeSession.catalogue
  if (!catalogue) return
  AccountCatalogue.finish(catalogue, { request_id: catalogue.requestId, sec_uid: catalogue.owner, mode: catalogue.mode, state: 'cancelled' })
  call('cancel_user_preview', catalogue.requestId)
}
async function openAccountHome(mode = state.forms.userMode, refresh = false) {
  const account = state.userAccount
  if (!account) return
  if (!account.is_self) { await openStreamerHome(account.sec_uid, 'user'); return }
  if (!['post', 'like'].includes(mode)) mode = 'like'
  if (!refresh && accountHomeSession && accountHomeSession.secUid === String(account.sec_uid)
      && accountHomeSession.contentMode === mode) {
    clearStreamerPreloads()
    state.streamer = accountHomeSession
    state.streamer.modal = null
    state.streamer.homeSection = mode
    state.streamer.returnPage = 'user'
    state.forms.userMode = mode
    state.page = 'streamer'
    resetPageScroll()
    render()
    return
  }
  const generation = ++accountHomeGeneration
  cancelCurrentAccountPreview()
  clearStreamerPreloads()
  const requestId = `${accountClientId}-${generation}`
  const catalogue = AccountCatalogue.create(String(account.sec_uid), mode, requestId, accountDownloadStates)
  state.userTaskProgress.browse = AccountTaskProgress.browse(null, {
    type: 'user_catalogue_page', request_id: requestId, scanned: 0, pages: 0,
    total_hint: Number(mode === 'like' ? account.favoriting_count : account.aweme_count) || 0,
  })
  const returnScroll = state.page === 'streamer' ? Number(state.streamer.returnScroll || 0) : window.scrollY || 0
  state.forms.userMode = mode
  state.page = 'streamer'
  state.streamer = { ...state.streamer, secUid: catalogue.owner, creator: account.creator || { ...account, avatar_url: account.avatar_data || '' }, works: catalogue.works, catalogue, contentMode: mode, homeSection: mode, supportedModes: ['post', 'like'], source: 'account', hasMore: true, loading: true, loadingMore: false, cursor: 0, offset: 0, mode: 'account', profileKnown: true, profileLoading: false, remoteCursors: new Set(), paginationStopped: false, error: '', profileError: '', returnPage: 'user', returnScroll, modal: null, tab: mode, subtab: mode, search: '', date: '' }
  accountHomeSession = state.streamer
  resetPageScroll()
  render()
  const accepted = await call('preview_user', { catalogue: true, request_id: requestId, client_id: accountClientId, request_order: generation, mode })
  if (generation !== accountHomeGeneration || accountHomeSession.catalogue !== catalogue) return
  if (accepted !== true) {
    AccountCatalogue.finish(catalogue, { request_id: requestId, sec_uid: catalogue.owner, mode, state: 'failed', error: '主页读取未能启动，请重试' })
    syncAccountHome()
  }
}
function syncAccountHome(changedIds) {
  const session = accountHomeSession
  const catalogue = session && session.catalogue
  if (!catalogue) return
  session.loading = catalogue.scanState === 'loading' && !catalogue.works.length
  session.loadingMore = catalogue.scanState === 'loading' && !!catalogue.works.length
  session.hasMore = catalogue.hasMore
  session.error = catalogue.error
  if (state.page !== 'streamer' || state.streamer !== session) return
  const view = creatorView()
  if (!view || !view.syncCatalogue || !view.syncCatalogue(changedIds)) renderPage()
}
function applyAccountEvent(event) {
  const catalogue = accountHomeSession && accountHomeSession.catalogue
  if (event.type === 'user_item_state' || event.type === 'user_file_progress') {
    const starting = state.userTaskProgress.starting
    const progress = starting && Number(event.generation) <= starting.generationFloor
      ? state.userTaskProgress.download : AccountTaskProgress.download(state.userTaskProgress.download, event)
    if (progress !== state.userTaskProgress.download) {
      state.userTaskProgress.download = progress
      state.userTaskProgress.starting = null
      renderStatus()
    }
    if (event.type === 'user_item_state' && AccountCatalogue.recordDownload(accountDownloadStates, event) && catalogue && catalogue.owner === String(event.sec_uid) && catalogue.mode === event.mode) {
      AccountCatalogue.refresh(catalogue, String(event.aweme_id))
      syncAccountHome([String(event.aweme_id)])
    }
    return true
  }
  if (event.type === 'user_download_scan') {
    const starting = state.userTaskProgress.starting
    const progress = starting && Number(event.generation) <= starting.generationFloor
      ? state.userTaskProgress.download : AccountTaskProgress.download(state.userTaskProgress.download, event)
    if (progress !== state.userTaskProgress.download) {
      state.userTaskProgress.download = progress
      state.userTaskProgress.starting = null
      renderStatus()
    }
    if (event.scan_state === 'failed') showToast(`账号下载扫描失败：${event.error || '请重试'}；已发现的内容仍会处理`)
    return true
  }
  if (!['user_catalogue_page', 'user_catalogue_state', 'user_catalogue_local_states'].includes(event.type)) return false
  if (!AccountCatalogue.matches(catalogue, event)) return true
  if (event.type === 'user_catalogue_page') AccountCatalogue.appendPage(catalogue, event)
  else if (event.type === 'user_catalogue_local_states') AccountCatalogue.localStates(catalogue, event)
  else AccountCatalogue.finish(catalogue, event)
  if (event.type !== 'user_catalogue_local_states') {
    state.userTaskProgress.browse = AccountTaskProgress.browse(state.userTaskProgress.browse, event)
    renderStatus()
  }
  syncAccountHome(event.type === 'user_catalogue_local_states' ? Object.keys(event.states || {}) : undefined)
  return true
}

async function resolveUserAccount(url, preferredMode = '', viewer = '') {
  if (state.busy.user || state.userAccountLoading) return false
  state.userAccountLoading = true
  setActivity('account', '读取账号')
  renderPage()
  const account = viewer
    ? await call('lock_user_account', null, viewer)
    : url === undefined
      ? await call('lock_user_account')
      : await call('lock_user_account', url)
  state.userAccountLoading = false
  if (!account || !account.sec_uid) {
    setActivity('account', '读取账号失败', { running: false })
    renderPage()
    return false
  }
  state.userAccount = account
  state.forms.userUrl = currentAccountUrl(account)
  const saved = state.settings.last_user_account || {}
  const rememberedMode = saved.sec_uid === account.sec_uid && saved.is_self === account.is_self
    ? saved.mode : ''
  const mode = preferredMode || rememberedMode
  state.forms.userMode = modeLabels[mode] && (account.is_self || !['collect', 'collectmix'].includes(mode))
    ? mode : account.is_self ? 'like' : 'post'
  const increaseByMode = state.settings.increase_by_mode || {}
  state.forms.userIncrease = modeSupportsIncremental(state.forms.userMode)
    && (Object.prototype.hasOwnProperty.call(increaseByMode, state.forms.userMode)
      ? !!increaseByMode[state.forms.userMode] : true)
  state.settings.last_user_account = { sec_uid: account.sec_uid, is_self: account.is_self,
    mode: state.forms.userMode, viewer: account.viewer || '' }
  setActivity('account', account.nickname || '抖音用户', { running: false })
  renderPage()
  return true
}

async function startAccountDownload(request) {
  if (state.busy.user || !state.userAccount) return false
  state.busy.user = true
  const floor = Number(String(state.userTaskProgress.download && state.userTaskProgress.download.task_id || '').split(':')[1]) || 0
  state.userTaskProgress.starting = { ...AccountTaskProgress.base('download:pending', 0),
    owner: String(state.userAccount.sec_uid), mode: String(request.mode), generationFloor: floor }
  renderStatus()
  if (state.page === 'streamer' && state.streamer.catalogue) syncAccountHome([])
  else renderPage()
  const accepted = await call('start_user_download', request)
  if (accepted !== true) {
    state.busy.user = false
    state.userTaskProgress.starting = null
    renderStatus()
    if (state.page === 'streamer' && state.streamer.catalogue) syncAccountHome([])
    else renderPage()
  }
  return accepted === true
}

// An unfinished download is only resumed after the user confirms it.
function showResumeUserDownloadPrompt() {
  const pending = state.settings.pending_user_download
  if (!pending || !pending.sec_uid || state.busy.user || document.querySelector('[data-resume-overlay]')) return
  const label = modeLabels[pending.mode] || '作品'
  const left = Number(pending.limit) > 0 ? `，还剩 ${Number(pending.remaining_limit ?? pending.limit)} 个` : ''
  document.body.insertAdjacentHTML('beforeend', `<div class="monitor-delete-overlay" data-resume-overlay><section class="monitor-delete-dialog" role="dialog" aria-modal="true" aria-labelledby="resume-title"><div class="monitor-delete-head"><h2 id="resume-title">继续上次的下载？</h2><button type="button" class="icon-button" data-ui-dismiss data-action="resume-user-later" aria-label="关闭">×</button></div><p>上次的「${esc(label)}」下载没有完成${esc(left)}。继续后会从上次的进度接着下载。</p><div class="monitor-delete-actions"><button type="button" class="button secondary" data-action="resume-user-discard">不再继续</button><button type="button" class="button primary" data-action="resume-user-download">继续下载</button></div></section></div>`)
}
function closeResumeUserDownloadPrompt() {
  const overlay = document.querySelector('[data-resume-overlay]')
  if (overlay) overlay.remove()
}

// QR login: keep the loading DOM stable so text updates never restart its spinner.
function qrLoginLoadingText(login) {
  return { loading: '正在启动扫码登录…', starting_browser: '正在启动浏览器…', loading_page: '正在加载抖音登录页…', loading_login_sdk: '正在初始化抖音登录组件…', requesting_qr: '正在申请登录二维码…', browser_fallback: '接口暂不可用，切换浏览器…' }[login.status] || '正在获取二维码…'
}
function qrLoginWindowTitle(login) {
  return login.status === 'loading' || login.status === 'starting_browser' ? '正在打开浏览器窗口' : '已打开浏览器窗口'
}
function qrLoginBodyKind(login) {
  if (login.status === 'error') return 'error'
  if (login.mode === 'window') return 'window'
  if (login.status === 'scanned') return 'scanned'
  return login.status === 'expired' || !login.image ? 'loading' : 'code'
}
function qrLoginBody(login) {
  if (login.status === 'error') {
    return `<div class="qr-login-message" role="alert"><strong>登录没有完成</strong><span data-qr-status-text>${esc(login.error || '扫码登录失败')}</span></div>`
  }
  if (login.mode === 'window') {
    return `<div class="qr-login-message"><span class="loading-spinner" aria-hidden="true"></span><strong data-qr-status-text>${qrLoginWindowTitle(login)}</strong><span>请在弹出的抖音页面中完成登录，完成后这里会自动关闭。</span></div>`
  }
  const cover = login.status === 'scanned'
    ? '<div class="qr-login-cover"><span class="qr-login-check" aria-hidden="true">✓</span><strong>扫码成功</strong><span>请在手机上确认登录</span></div>'
    : login.status === 'expired' || !login.image
      ? `<div class="qr-login-cover is-loading"><span class="loading-spinner" aria-hidden="true"></span><span data-qr-status-text>${login.image ? '二维码已过期，正在刷新…' : qrLoginLoadingText(login)}</span></div>`
      : ''
  const image = login.image ? `<img src="${esc(login.image)}" alt="抖音登录二维码" draggable="false">` : ''
  const hint = login.image ? '打开「抖音 App」，点击左上角 ☰ 扫一扫' : '二维码由抖音官方接口申请'
  return `<div class="qr-login-frame${login.image ? '' : ' is-empty'}">${image}${cover}</div><p class="qr-login-hint">${hint}</p>`
}
function renderQrLogin() {
  const existing = document.querySelector('[data-qr-login-overlay]')
  const login = state.qrLogin
  if (!login) { if (existing) existing.remove(); return }
  const focused = existing && existing.contains(document.activeElement) && document.activeElement.dataset
    ? document.activeElement.dataset.action : ''
  const failed = login.status === 'error'
  const actions = failed
    ? `<button type="button" class="button secondary" data-action="qr-login-window">改用浏览器窗口</button>${button('重新获取二维码', 'qr-login-retry', 'primary')}`
    : login.mode === 'window'
      ? `<button type="button" class="button secondary" data-ui-dismiss data-action="qr-login-close">取消登录</button>`
      : `<button type="button" class="link-button qr-login-switch" data-action="qr-login-window">二维码显示不出来？改用浏览器窗口</button>`
  const html = `<div class="monitor-delete-overlay" data-qr-login-overlay><section class="monitor-delete-dialog qr-login-dialog" role="dialog" aria-modal="true" aria-labelledby="qr-login-title"><div class="monitor-delete-head"><h2 id="qr-login-title">扫码登录抖音</h2><button type="button" class="icon-button" data-ui-dismiss data-action="qr-login-close" aria-label="关闭">×</button></div><div class="qr-login-body" aria-live="polite">${qrLoginBody(login)}</div><div class="qr-login-actions">${actions}</div></section></div>`
  if (!existing) document.body.insertAdjacentHTML('beforeend', html)
  const overlay = existing || document.querySelector('[data-qr-login-overlay]')
  const bodyKind = qrLoginBodyKind(login)
  const actionKind = failed ? 'error' : login.mode
  if (existing) {
    const body = overlay.querySelector('.qr-login-body')
    if (overlay.dataset.qrBodyKind !== bodyKind) {
      body.innerHTML = qrLoginBody(login)
    } else {
      const text = body.querySelector('[data-qr-status-text]')
      const message = failed ? login.error || '扫码登录失败'
        : login.mode === 'window' ? qrLoginWindowTitle(login)
          : login.image ? '二维码已过期，正在刷新…' : qrLoginLoadingText(login)
      if (text && text.textContent !== message) text.textContent = message
      const image = body.querySelector('img')
      if (image && login.image && image.getAttribute('src') !== login.image) image.src = login.image
    }
    if (overlay.dataset.qrActionKind !== actionKind) {
      overlay.querySelector('.qr-login-actions').innerHTML = actions
    }
  }
  overlay.dataset.qrBodyKind = bodyKind
  overlay.dataset.qrActionKind = actionKind
  const again = focused && overlay.querySelector(`[data-action="${focused}"]`)
  if (again && document.activeElement !== again) again.focus({ preventScroll: true })
  else if (!existing || (focused && !again)) focusModal(overlay.querySelector('[role="dialog"]'))
}
async function startQrLogin(inWindow = false) {
  state.qrLogin = { mode: inWindow ? 'window' : 'app', status: 'loading', image: '', error: '' }
  renderQrLogin()
  const accepted = await call('start_qr_login', inWindow)
  if (!accepted && state.qrLogin) {
    state.qrLogin = { ...state.qrLogin, status: 'error', error: '扫码登录未能启动' }
    renderQrLogin()
  }
}
function closeQrLogin() {
  if (!state.qrLogin) return
  state.qrLogin = null
  renderQrLogin()
  setActivity('login', '')
  call('cancel_qr_login')
}
async function restoreLastUserSession(resume = false) {
  if (state.userAccountLoading || state.busy.user) return false
  if (state.userAccount && !resume) return false
  const pending = state.settings.pending_user_download
  const remembered = state.settings.last_user_account
  if (!state.userAccount) {
    const saved = (pending && pending.sec_uid && pending)
      || (remembered && remembered.sec_uid && remembered)
      || (state.settings.cookie_configured && state.settings.mode === 'like'
        ? { sec_uid: 'self', is_self: true, mode: 'like' } : null)
    if (!saved || !/^[A-Za-z0-9_-]+$/.test(String(saved.sec_uid || ''))) return false
    const restored = await resolveUserAccount(saved.is_self ? undefined : currentAccountUrl(saved), saved.mode, String(saved.viewer || ''))
    if (!restored) return false
  }
  if (resume && pending && pending.sec_uid === state.userAccount.sec_uid
      && pending.is_self === state.userAccount.is_self && modeLabels[pending.mode]
      && (state.userAccount.is_self || !['collect', 'collectmix'].includes(pending.mode))
      && !(Number(pending.limit) > 0 && Number(pending.remaining_limit) === 0)) {
    state.forms.userMode = pending.mode
    state.forms.userLimit = Math.max(0, Number(pending.remaining_limit ?? pending.limit) || 0)
    state.forms.userIncrease = !!pending.increase
    state.forms.path = String(pending.path || state.forms.path)
    state.forms.startTime = String(pending.start_time || '')
    state.forms.endTime = String(pending.end_time || '')
    await startAccountDownload({ url: userRequestUrl(), mode: pending.mode,
      limit: state.forms.userLimit, increase: state.forms.userIncrease, path: state.forms.path,
      start_time: state.forms.startTime, end_time: state.forms.endTime })
  }
  return true
}
function monitorWorkCell(row) {
  const title = String(row.latest_aweme_title || '').replace(/\s+/g, ' ').trim()
  const published = Number(row.latest_aweme_time || 0)
  if (!title && published <= 0) return '<td class="work">—</td>'
  const label = title || '无标题'
  const hint = published > 0 ? `${label} · 发布 ${formatMonitorTime(published)}` : label
  return `<td class="work" title="${esc(hint)}">${esc(label)}</td>`
}
function monitorStatus(row) {
  const result = (text, detail, tone, error = false) => {
    const details = (Array.isArray(detail) ? detail : [detail]).filter(Boolean)
    return {
      text,
      title: details.length ? [text, ...details].join(' · ') : '',
      tone,
      error,
    }
  }
  if (row._pending) return result('解析中', '', 'info')
  const invisibleCount = Math.max(0, Number(row.invisible_count || 0))
  const retryableCount = Math.max(0, Number(row.retryable_failed_count || 0))
  const permanentCount = Math.max(0, Number(row.permanent_failed_count || 0))
  const pendingDownloadCount = Math.max(0, Number(row.pending_download_count || 0))
  const downloadingCount = Math.max(0, Number(row.downloading_count || 0))
  const historical = []
  if (invisibleCount) historical.push('不可见 ' + invisibleCount)
  const visiblePermanent = Math.max(0, permanentCount - invisibleCount)
  if (visiblePermanent) historical.push('历史失败 ' + visiblePermanent)
  const structuredTitle = [
    String(row.last_work_error_message || '').trim(),
    historical.join(' · '),
  ].filter(Boolean).join(' · ')
  const error = String(row.last_error || '').trim()
  const note = String(row.last_check_note || '').trim()
  const workOnlyError = /(不可见|下载失败|待重试|作品|详情|下载地址|HTTP\s*\d{3})/i.test(error)
  const hasGlobalToken = /(cookie|登录|接口|数据库|写入|目录|磁盘|分页|风控|检查失败|任务已停止|未建立基线)/i.test(error)
  const globalError = error && (hasGlobalToken || !workOnlyError) && (
    !retryableCount && !downloadingCount
      ? true
      : hasGlobalToken
  )
  if (globalError) return result('异常', error, 'error', true)

  const progress = note.match(/^(?:下载中|全量备份中|自动下载中)\s*[：:]?\s*(\d+)\s*\/\s*(\d+)$/)
  if (progress) return result('下载中', [
    progress[1] + '/' + progress[2],
    !row.enabled ? '监控暂停，下载继续' : '',
  ], 'info')

  if (downloadingCount || note.match(/^(?:下载中|全量备份中|自动下载中)[：:]/)) {
    const noteDetail = note.replace(/^(?:下载中|全量备份中|自动下载中)[：:]?\s*/, '')
    return result('下载中', [
      downloadingCount ? downloadingCount + ' 个作品' : noteDetail,
      !row.enabled ? '监控暂停，下载继续' : '',
    ], 'info')
  }
  const scanned = note.match(/^扫描中[：:]\s*已扫描\s*(\d+)\s*个作品$/)
  if (scanned) return result('扫描中', '已扫描 ' + scanned[1] + ' 个作品', 'info')
  if (note === '检查中' || note === '检查中…') {
    return result('扫描中', '已扫描 0 个作品', 'info')
  }
  if (retryableCount) {
    return result('待重试', [
      retryableCount + ' 个作品',
      !row.enabled ? '监控暂停，队列仍会重试' : '',
      structuredTitle || note,
    ], 'warn')
  }
  if (pendingDownloadCount) {
    return result('排队中', [
      pendingDownloadCount + ' 个作品',
      !row.enabled ? '监控暂停，下载继续' : '',
    ], 'info')
  }
  if (!row.enabled) return result('已暂停', '', 'muted')
  if (/(异常：|检查失败|检查不完整|全量备份未完成)/.test(note) && !/(不可见|下载失败|待重试|作品详情)/.test(note)) {
    return result('异常', note.replace(/^异常[：:]\s*/, ''), 'error', true)
  }
  return result('正常', '', 'ok')
}
function monitorColumnCell(key, row, status) {
  if (key === 'monitor') {
    return `<td class="switch-cell"><button type="button" class="switch${row.enabled ? ' on' : ''}" data-action="monitor-toggle" data-id="${esc(row.sec_uid)}" aria-pressed="${!!row.enabled}"${row._pending ? ' disabled' : ''}><i></i></button></td>`
  }
  if (key === 'comments') {
    return `<td class="switch-cell"><button type="button" class="switch${row.comments_enabled ? ' on' : ''}" data-action="monitor-comments-toggle" data-id="${esc(row.sec_uid)}" aria-pressed="${!!row.comments_enabled}" aria-label="保存评论：${esc(row.nickname || '未知主播')}"${row._pending ? ' disabled' : ''}><i></i></button></td>`
  }
  if (key === 'nickname') {
    const nickname = String(row.nickname || '未知主播')
    return `<td class="title"><button type="button" class="creator-link" title="${esc(nickname)}" aria-label="打开主播主页：${esc(nickname)}" data-action="streamer-open" data-id="${esc(row.sec_uid)}"${row._pending ? ' disabled' : ''}>${esc(nickname)}</button></td>`
  }
  if (key === 'status') {
    const title = status.title ? ' title="' + esc(status.title) + '"' : ''
    return '<td class="status-cell"><span class="status-badge ' + (status.tone || '') + '"' + title + '><span class="status-text">' + esc(status.text) + '</span></span></td>'
  }
  if (key === 'lastCheck') return `<td data-ts="${Number(row.last_check_at || 0)}">${formatMonitorTime(row.last_check_at)}</td>`
  if (key === 'latestWork') return monitorWorkCell(row)
  if (key === 'publicCount') {
    const profileCount = Number(row.profile_aweme_count ?? row.public_aweme_count ?? 0)
    return `<td class="num">${profileCount > 0 ? profileCount : '—'}</td>`
  }
  if (key === 'last24h') return `<td class="num">${Number(row.last_24h_downloaded || 0)}</td>`
  if ((key === 'localCount' || key === 'storage' || key === 'commentsBytes') && row.local_stats_pending) return '<td class="num" title="正在统计本地文件">…</td>'
  if (key === 'localCount') return `<td class="num">${Number(row.local_work_count || 0)}</td>`
  if (key === 'storage') {
    const rawBytes = Number(row.local_storage_bytes || 0)
    const bytes = Number.isFinite(rawBytes) ? Math.max(0, Math.floor(rawBytes)) : 0
    return `<td class="num" title="${bytes} 字节">${formatMonitorStorageBytes(bytes)}</td>`
  }
  if (key === 'commentsBytes') {
    const rawBytes = Number(row.comments_bytes || 0)
    const bytes = Number.isFinite(rawBytes) ? Math.max(0, Math.floor(rawBytes)) : 0
    return bytes ? `<td class="num" title="${bytes} 字节">${formatMonitorBytes(bytes)}</td>` : '<td class="num monitor-muted">—</td>'
  }
  if (row.local_stats_pending && (key === 'hiddenWorks' || monitorColumnsHiddenByDefault.includes(key))) {
    return `<td${key === 'latestPublish' || key === 'douyinId' ? '' : ' class="num"'} title="正在统计">…</td>`
  }
  if (key === 'hiddenWorks') {
    const count = Number(row.hidden_work_count || 0)
    return `<td class="num${count ? '' : ' monitor-muted'}">${count}</td>`
  }
  if (key === 'newWorks') return `<td class="num">${Number((state.monitorNewWindow === 7 ? row.new_7d_count : row.new_1d_count) || 0)}</td>`
  if (key === 'latestPublish') {
    const ts = Number(row.latest_publish_time || row.latest_aweme_time || 0)
    const title = ts > 0 ? ` title="${esc(new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }))}"` : ''
    return `<td data-ts="${ts}"${title}>${formatMonitorTime(ts)}</td>`
  }
  if (key === 'idleDays') {
    const days = monitorIdleDays(row.latest_publish_time || row.latest_aweme_time)
    if (days == null) return '<td class="num monitor-muted">—</td>'
    const stale = days >= 30
    return `<td class="num${stale ? ' monitor-muted' : ''}"${stale ? ' title="超过 30 天没有发布新作品"' : ''}>${days ? days + '天' : '今天'}</td>`
  }
  if (key === 'mediaMix') return `<td class="num">${Number(row.video_count || 0)} / ${Number(row.gallery_count || 0)}</td>`
  if (key === 'videoDuration') return `<td class="num">${formatMonitorDuration(row.video_duration_ms)}</td>`
  if (key === 'topDigg') {
    const count = Number(row.top_digg_count || 0)
    const title = row.top_digg_title ? ` title="${esc(row.top_digg_title)}：${count} 赞"` : ''
    return `<td class="num"${title}>${count ? formatMonitorCount(count) : '—'}</td>`
  }
  if (key === 'avgDigg') {
    const count = Number(row.avg_digg_count || 0)
    return `<td class="num"${count ? ` title="${count} 赞"` : ''}>${count ? formatMonitorCount(count) : '—'}</td>`
  }
  if (key === 'commentsSaved') {
    const done = Number(row.downloaded_work_count || 0)
    return `<td class="num">${done ? Number(row.comments_saved_count || 0) + ' / ' + done : '—'}</td>`
  }
  if (key === 'douyinId') {
    const id = String(row.douyin_id || '')
    return `<td class="monitor-douyin-id" title="${esc(id)}">${id ? esc(id) : '—'}</td>`
  }
  return ''
}
function monitorActionCell(row) {
  const id = String(row.sec_uid || '')
  const open = state.monitorMenuId === id
  const disabled = row._pending || state.pendingMonitorDeletes.has(id)
  return `<td class="monitor-actions-cell"><button type="button" class="icon-button monitor-row-menu-trigger" data-action="monitor-menu-toggle" data-id="${esc(id)}" aria-label="主播操作：${esc(row.nickname || '未知主播')}" aria-haspopup="menu" aria-expanded="${open}"${disabled ? ' disabled' : ''}>⋯</button></td>`
}
function monitorColumnPicker() {
  const rows = state.monitorColumns.map(key => {
    const column = monitorColumnSpecs[key]
    if (!column) return ''
    const checked = !state.hiddenMonitorColumns.has(key)
    const windowSelect = key === 'newWorks'
      ? `<select class="column-picker-select" data-monitor-new-window aria-label="新增作品统计范围"><option value="1"${state.monitorNewWindow === 7 ? '' : ' selected'}>1天</option><option value="7"${state.monitorNewWindow === 7 ? ' selected' : ''}>7天</option></select>`
      : ''
    return `<div class="column-picker-row" data-column-row data-column="${key}"><label class="column-picker-label"><input type="checkbox" data-action="toggle-monitor-column" data-column="${key}"${checked ? ' checked' : ''} aria-label="显示${column.label}">${column.label}</label>${windowSelect}<span class="column-drag-handle" title="拖拽排序" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="7" cy="8" r="1"/><circle cx="12" cy="8" r="1"/><circle cx="17" cy="8" r="1"/><circle cx="7" cy="16" r="1"/><circle cx="12" cy="16" r="1"/><circle cx="17" cy="16" r="1"/></svg></span></div>`
  }).join('')
  return `<div class="column-picker" data-column-picker role="dialog" aria-label="显示列"><div class="column-picker-head"><strong>显示列</strong><button type="button" class="icon-button column-picker-close" data-action="close-column-picker" aria-label="关闭">×</button></div><div class="column-picker-list">${rows}</div></div>`
}
function monitorRows() {
  if (!state.monitors.length) return state.errors.monitor
    ? `<div class="empty"><strong>读取失败</strong><span>${esc(state.errors.monitor)}</span><button type="button" class="button secondary" data-action="monitor-refresh">重试</button></div>`
    : emptyState('还没有关注主播', '点「导入关注列表」从你的抖音账号导入，或者用「手动添加」粘贴主页链接')
  const columns = state.monitorColumns.filter(key => !state.hiddenMonitorColumns.has(key)).map(key => monitorColumnSpecs[key]).filter(Boolean)
  if (!columns.length) return emptyState('请至少显示一列')
  const visibleKeys = state.monitorColumns.filter(key => !state.hiddenMonitorColumns.has(key))
  // The remaining columns stay configurable: 最新作品、抖音页作品数、24h内下载、本地作品数量、存储占用.
  const selectable = state.monitors.filter(row => !row._pending)
  const allSelected = selectable.length > 0 && selectable.every(row => row._selected)
  const head = `<tr><th class="monitor-select-heading" style="width:48px"><button type="button" class="check${allSelected ? ' is-checked' : ''}" data-action="monitor-select-all" aria-label="全选关注主播" aria-pressed="${allSelected ? 'true' : selectable.some(row => row._selected) ? 'mixed' : 'false'}"></button></th>${/* width:66px keeps the switch cell wide enough */ visibleKeys.map(key => {
    const column = monitorColumnSpecs[key]
    if (!column) return ''
    const className = column.className ? ` class="${column.className}"` : ''
    const width = column.width ? ` style="width:${column.width}"` : ''
    const title = column.title ? ` title="${esc(column.title)}"` : ''
    return `<th${className}${width}${title}>${monitorColumnLabel(key)}</th>`
  }).join('')}<th class="monitor-actions-heading" style="width:68px"></th></tr>`
  const body = state.monitors.map(row => {
    const status = monitorStatus(row)
    const cells = `<td class="monitor-select-cell"><input class="monitor-row-select" type="checkbox" data-action="monitor-select" data-id="${esc(row.sec_uid)}" aria-label="选择${esc(row.nickname || '未知主播')}"${row._selected ? ' checked' : ''}${row._pending ? ' disabled' : ''}></td>` + visibleKeys.map(key => monitorColumnCell(key, row, status)).join('') + monitorActionCell(row)
    const rowClass = [row._selected ? 'selected' : '', String(row.sec_uid) === state.monitorHoverId ? 'is-hovered' : ''].filter(Boolean).join(' ')
    return `<tr class="${rowClass}" aria-selected="${!!row._selected}" data-action="monitor-select" data-id="${esc(row.sec_uid)}">${cells}</tr>`
  }).join('')
  return `<div class="table-wrap" data-ui-scroll-key="monitor-table"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>`
}
// Keep row nodes, horizontal position and keyboard focus stable on selection.
function syncMonitorSelection() {
  const rows = new Map(state.monitors.map(row => [String(row.sec_uid), row]))
  document.querySelectorAll('.monitor-panel tbody tr[data-id]').forEach(node => {
    const row = rows.get(node.dataset.id)
    if (row) AppUI.setRowSelected(node, !!row._selected)
  })
  const selectable = state.monitors.filter(row => !row._pending)
  const all = selectable.length > 0 && selectable.every(row => row._selected)
  const some = selectable.some(row => row._selected)
  document.querySelectorAll('[data-action="monitor-select-all"]').forEach(button => {
    button.classList.toggle('is-checked', all)
    button.setAttribute('aria-pressed', some && !all ? 'mixed' : String(all))
  })
  const scope = document.querySelector('[data-monitor-scope]')
  if (scope) scope.textContent = monitorActionScopeLabel()
}
function syncMonitorValidation() {
  const button = document.querySelector('[data-action="monitor-validate-local"]')
  if (!button) return
  const progress = state.localValidate || { scanned: 0, total: 0, busy: false }
  button.style.setProperty('--progress', `${progress.total > 0 ? Math.max(0, Math.min(100, progress.scanned * 100 / progress.total)) : 0}%`)
  button.disabled = !!progress.busy
  button.setAttribute('aria-busy', String(!!progress.busy))
  const label = button.querySelector('span')
  if (label) label.textContent = progress.busy ? '正在校验…' : '校验本地文件'
}
function monitorRowMenu() {
  const id = state.monitorMenuId
  const row = state.monitors.find(item => String(item.sec_uid) === id)
  if (!id || !row || row._pending) return ''
  const nickname = String(row.nickname || '未知主播')
  return `<div id="monitor-row-menu" class="monitor-row-menu" role="menu" aria-label="${esc(nickname)}的操作" data-monitor-row-menu><button type="button" role="menuitem" data-action="monitor-check-latest" data-id="${esc(id)}">检查更新</button><button type="button" role="menuitem" data-action="monitor-full-scan-one" data-id="${esc(id)}">全量扫描</button><div class="monitor-row-menu-divider" aria-hidden="true"></div><button type="button" class="danger-text" role="menuitem" data-action="monitor-delete-open" data-id="${esc(id)}">删除主播</button></div>`
}
function monitorDeleteDialog() {
  const request = state.monitorDelete
  if (!request) return ''
  const single = request.ids.length === 1
  const subject = single
    ? (request.names[0] || '该主播')
    : `已选中的 ${request.ids.length} 个主播`
  const localLabel = single
    ? '同时删除已下载的本地文件'
    : '同时删除这些主播已下载的本地文件'
  const deleteLabel = single ? '删除主播' : `删除 ${request.ids.length} 个主播`
  const list = single ? '' : `<ul class="monitor-delete-list" aria-label="将要删除的主播">${request.ids.map((id, index) => {
    const row = state.monitors.find(item => String(item.sec_uid) === id) || {}
    const bytes = Math.max(0, Math.floor(Number(row.local_storage_bytes || 0)) || 0)
    const stats = row.local_stats_pending ? '正在统计…' : `${Number(row.local_work_count || 0)} 个作品 · ${formatMonitorStorageBytes(bytes)}`
    return `<li><span>${esc(request.names[index] || '未知主播')}</span><small>${stats}</small></li>`
  }).join('')}</ul>`
  return `<div class="monitor-delete-overlay" data-monitor-delete-overlay><section class="monitor-delete-dialog" role="dialog" aria-modal="true" aria-labelledby="monitor-delete-title"><div class="monitor-delete-head"><h2 id="monitor-delete-title">${deleteLabel}</h2><button type="button" class="icon-button" data-ui-dismiss data-action="monitor-delete-cancel" aria-label="关闭"${request.busy ? ' disabled' : ''}>×</button></div><p>从关注列表移除${esc(subject)}？</p>${list}<label class="monitor-delete-option"><input type="checkbox" data-action="monitor-delete-local-toggle"${request.deleteLocalWorks ? ' checked' : ''}${request.busy ? ' disabled' : ''}><span><strong>${localLabel}</strong></span></label>${request.error ? `<p class="monitor-delete-error" role="alert">${esc(request.error)}</p>` : ''}<div class="monitor-delete-actions"><button type="button" class="button secondary" data-ui-dismiss data-action="monitor-delete-cancel"${request.busy ? ' disabled' : ''}>取消</button><button type="button" class="button danger" data-action="monitor-delete-confirm"${request.busy ? ' disabled' : ''}>${request.busy ? '正在提交…' : deleteLabel}</button></div></section></div>`
}
function alignMonitorRowMenu() {
  const menu = byId('monitor-row-menu')
  if (!menu) return
  const trigger = [...document.querySelectorAll('[data-action="monitor-menu-toggle"]')]
    .find(node => node.dataset.id === state.monitorMenuId)
  if (!trigger) return
  const rect = trigger.getBoundingClientRect()
  const bounds = menu.getBoundingClientRect()
  AppUI.rememberMenuAnchor(menu, trigger)
  const top = rect.bottom + bounds.height + 8 <= window.innerHeight
    ? rect.bottom + 4
    : Math.max(8, rect.top - bounds.height - 4)
  const left = Math.max(8, Math.min(window.innerWidth - bounds.width - 8, rect.right - bounds.width))
  menu.style.top = `${top}px`
  menu.style.left = `${left}px`
}
function closeMonitorRowMenu() {
  const id = state.monitorMenuId
  if (!id) return ''
  state.monitorMenuId = ''
  const trigger = [...document.querySelectorAll('[data-action="monitor-menu-toggle"]')]
    .find(node => node.dataset.id === id)
  if (trigger) trigger.setAttribute('aria-expanded', 'false')
  byId('monitor-row-menu')?.remove()
  return id
}
function openMonitorDelete(ids) {
  const rows = ids.map(id => state.monitors.find(row => String(row.sec_uid) === String(id))).filter(Boolean)
  if (!rows.length || rows.some(row => row._pending || state.pendingMonitorDeletes.has(String(row.sec_uid)))) return
  const menuId = state.monitorMenuId
  state.dialogFocusReturn = menuId
    ? { action: 'monitor-menu-toggle', id: menuId, tag: 'BUTTON' }
    : { action: 'monitor-delete', tag: 'BUTTON' }
  state.monitorMenuId = ''
  state.monitorDelete = {
    ids: rows.map(row => String(row.sec_uid)),
    names: rows.map(row => String(row.nickname || '未知主播')),
    deleteLocalWorks: false,
    busy: false,
    error: '',
  }
  renderPage()
}
function followingImportControl() {
  const current = state.followingImport || {}
  if (current.phase === 'loading') return button('正在读取…', 'following-toggle', 'primary', 'disabled', true)
  if (current.phase === 'importing') return button(current.open ? '正在导入…' : '查看导入进度', 'following-toggle', 'primary')
  return button('导入关注列表', 'following-toggle', 'primary')
}
function followingAvatar(item) {
  const nickname = String(item.nickname || '抖').trim() || '抖'
  if (item.avatar_url) return `<img class="following-avatar" src="${esc(item.avatar_url)}" alt="${esc(nickname)}">`
  return `<span class="following-avatar following-avatar-fallback">${esc(nickname.slice(0, 1))}</span>`
}
function libraryAccounts() {
  return (Array.isArray(state.settings.accounts) ? state.settings.accounts : []).filter(item => item.sec_uid && !item.expired)
}
function accountSelect(attr, selected, label) {
  const accounts = libraryAccounts()
  if (accounts.length < 2) return ''
  const current = String(selected || '')
  const options = accounts.map(item => {
    const value = item.is_main ? '' : String(item.sec_uid)
    return `<option value="${esc(value)}"${value === current ? ' selected' : ''}>${esc(item.nickname || '抖音用户')}${item.is_main ? '（主账号）' : ''}</option>`
  }).join('')
  return `<select class="account-select" ${attr} aria-label="${esc(label)}">${options}</select>`
}
function followingAccountPicker(current, disabled = false) {
  const picker = accountSelect(`data-following-account${disabled ? ' disabled' : ''}`, current.accountId, '从哪个账号导入')
  return picker ? `<label class="following-account-picker"><span>导入账号</span>${picker}</label>` : ''
}
async function startFollowingFetch(accountId) {
  const current = state.followingImport
  if (current.requestId) current.staleIds = new Set([...(current.staleIds || []), current.requestId])
  current.accountId = String(accountId || '')
  current.open = true; current.phase = 'loading'; current.requestId = ''; current.loaded = 0; current.pages = 0; current.totalHint = 0; current.account = {}; current.items = []; current.selected = new Set(); current.page = 1; current.error = ''
  renderPage()
  const accepted = await call('fetch_following', current.accountId)
  if (!accepted) { current.phase = 'error'; current.error = '关注列表读取未能启动'; renderPage(); return }
  if (typeof accepted === 'string' && current.phase === 'loading') current.requestId = accepted
}
function followingRequestMatches(current, event) {
  const id = String(event.request_id || '')
  if (current.staleIds && current.staleIds.has(id)) return false
  if (!current.requestId) current.requestId = id
  return id === String(current.requestId || '')
}
function followingImportModal() {
  const current = state.followingImport || {}
  if (!current.open) return ''
  const phase = current.phase || 'idle'
  if (phase === 'loading') {
    const total = Number(current.totalHint || 0)
    const progress = total > 0 ? `已获取 ${Number(current.loaded || 0)} / ${total} 人` : `已获取 ${Number(current.loaded || 0)} 人`
    return `<div class="following-overlay" role="dialog" aria-modal="true" aria-labelledby="following-import-title"><section class="following-dialog" tabindex="-1"><div class="following-dialog-head"><div><h2 id="following-import-title">导入关注主播</h2></div>${followingAccountPicker(current)}</div><div class="following-dialog-loading"><span class="loading-spinner" aria-hidden="true"></span><strong>正在读取关注列表…</strong><span>${progress}</span></div><div class="following-dialog-actions"><button type="button" class="button secondary" data-ui-dismiss data-action="following-cancel">取消</button></div></section></div>`
  }
  if (phase === 'error') {
    return `<div class="following-overlay" role="dialog" aria-modal="true" aria-labelledby="following-import-title"><section class="following-dialog" tabindex="-1"><div class="following-dialog-head"><div><h2 id="following-import-title">导入关注主播</h2></div>${followingAccountPicker(current)}<button type="button" class="icon-button" data-ui-dismiss data-action="following-close" aria-label="关闭">×</button></div><div class="notice error following-dialog-error" role="alert">${esc(current.error || '关注列表读取失败')}</div><div class="following-dialog-actions"><button type="button" class="button secondary" data-ui-dismiss data-action="following-close">关闭</button><button type="button" class="button primary" data-action="following-retry">重试</button></div></section></div>`
  }
  if (phase === 'importing') {
    return `<div class="following-overlay" role="dialog" aria-modal="true" aria-labelledby="following-import-title"><section class="following-dialog" tabindex="-1"><div class="following-dialog-head"><div><h2 id="following-import-title">导入关注主播</h2></div><button type="button" class="icon-button" data-ui-dismiss data-action="following-close" aria-label="暂时关闭导入进度">×</button></div><div class="following-dialog-loading"><span class="loading-spinner" aria-hidden="true"></span><strong>正在导入…</strong></div><div class="following-dialog-actions"><button type="button" class="button secondary" data-ui-dismiss data-action="following-close">在后台继续</button></div></section></div>`
  }
  const items = Array.isArray(current.items) ? current.items : []
  const importable = items.filter(item => item && item.can_import)
  const selected = current.selected || new Set()
  const allSelected = importable.length > 0 && importable.every(item => selected.has(String(item.sec_uid)))
  const pageSize = 50
  const maxPage = Math.max(1, Math.ceil(items.length / pageSize))
  const page = Math.min(maxPage, Math.max(1, Number(current.page || 1)))
  const pageItems = items.slice((page - 1) * pageSize, page * pageSize)
  const rows = pageItems.length ? pageItems.map(item => {
    const id = String(item.sec_uid || '')
    const existing = !item.can_import
    const checked = selected.has(id)
    const checkbox = `<button type="button" class="check${checked ? ' is-checked' : ''}" data-action="following-toggle-row" data-id="${esc(id)}" aria-pressed="${checked}" aria-label="${existing ? '已添加' : checked ? '取消选择' : '选择'}${existing ? '' : '主播'}"${existing ? ' disabled' : ''}></button>`
    return `<tr class="${existing ? 'following-existing' : ''}" aria-selected="${checked}"><td class="following-check-cell">${checkbox}</td><td><div class="following-person">${followingAvatar(item)}<div class="following-person-copy"><strong title="${esc(item.nickname || '')}">${esc(item.nickname || '未知主播')}</strong>${item.unique_id ? `<span>抖音号：${esc(item.unique_id)}</span>` : ''}</div></div></td><td class="following-signature" title="${esc(item.signature || '')}">${esc(item.signature || '—')}</td><td>${existing ? '<span class="status-badge muted">已添加</span>' : ''}</td></tr>`
  }).join('') : `<tr><td colspan="4">${emptyState('没有可导入的主播')}</td></tr>`
  const pageButtons = maxPage > 1 ? `<div class="following-pages"><button type="button" class="button secondary compact" data-action="following-page-prev"${page <= 1 ? ' disabled' : ''}>上一页</button><span>第 ${page} / ${maxPage} 页</span><button type="button" class="button secondary compact" data-action="following-page-next"${page >= maxPage ? ' disabled' : ''}>下一页</button></div>` : ''
  const importError = current.error ? `<div class="notice error following-dialog-error" role="alert">${esc(current.error)}</div>` : ''
  return `<div class="following-overlay" role="dialog" aria-modal="true" aria-labelledby="following-import-title"><section class="following-dialog following-dialog-ready" tabindex="-1"><div class="following-dialog-head"><div><h2 id="following-import-title">导入关注主播</h2><p>${current.account && current.account.nickname ? `${esc(current.account.nickname)} 的关注 · ` : ''}共 ${items.length} 人</p></div>${followingAccountPicker(current)}<button type="button" class="icon-button" data-ui-dismiss data-action="following-close" aria-label="关闭">×</button></div>${importError}<div class="following-dialog-toolbar"><label class="following-select-all"><button type="button" class="check${allSelected ? ' is-checked' : ''}" data-action="following-select-all" aria-label="全选关注主播" aria-pressed="${allSelected ? 'true' : selected.size ? 'mixed' : 'false'}"${importable.length ? '' : ' disabled'}></button><span>全选</span></label><span class="label" data-following-selected>已选 ${selected.size} 人</span><span class="spacer"></span>${pageButtons}</div><div class="following-list-wrap" data-ui-scroll-key="following-page-${current.page}"><table class="following-list"><thead><tr><th style="width:54px"></th><th>主播</th><th>简介</th><th style="width:100px"></th></tr></thead><tbody>${rows}</tbody></table></div><div class="following-dialog-actions"><span class="spacer"></span><button type="button" class="button secondary" data-ui-dismiss data-action="following-close">取消</button><button type="button" class="button primary" data-action="following-import"${selected.size ? '' : ' disabled'}>确认导入</button></div></section></div>`
}
function manualMonitorImportModal() {
  if (!state.monitorAddOpen) return ''
  const submitting = !!state.monitorAddSubmitting
  return `<div class="following-overlay monitor-add-overlay" data-monitor-add-overlay role="dialog" aria-modal="true" aria-labelledby="monitor-add-title"><section class="following-dialog monitor-add-dialog" tabindex="-1"><div class="following-dialog-head"><div><h2 id="monitor-add-title">手动添加主播</h2></div><button type="button" class="icon-button" data-ui-dismiss data-action="monitor-add-close" aria-label="关闭"${submitting ? ' disabled' : ''}>×</button></div><div class="monitor-add-body"><textarea data-bind="monitorUrls" aria-label="主播主页链接，每行一个" placeholder="每行一个主播主页链接"${submitting ? ' disabled' : ''}>${esc(state.forms.monitorUrls || '')}</textarea></div><div class="following-dialog-actions"><span class="spacer"></span><button type="button" class="button secondary" data-ui-dismiss data-action="monitor-add-close"${submitting ? ' disabled' : ''}>取消</button>${button(submitting ? '正在提交…' : '添加', 'monitor-add', 'primary', submitting ? 'disabled' : '', submitting)}</div></section></div>`
}
function closeManualMonitorImport() {
  if (state.monitorAddSubmitting) return
  state.monitorAddOpen = false
  renderPage()
}
function monitorActionTargets() {
  const usable = state.monitors.filter(item => !item._pending && !state.pendingMonitorDeletes.has(String(item.sec_uid)))
  const picked = usable.filter(item => item._selected)
  const selected = picked.length > 0
  const ids = (selected ? picked : usable).filter(item => item.enabled).map(item => item.sec_uid)
  return { selected, ids, scope: selected ? `已选 ${ids.length} 个主播` : `全部 ${ids.length} 个主播` }
}
function monitorActionScopeLabel() {
  const total = state.monitors.length
  const picked = state.monitors.filter(item => item._selected && !item._pending).length
  return picked ? `已选 ${picked} / ${total} 个主播` : `共 ${total} 个主播，未勾选时操作全部`
}
function fullScanScheduleTitle() {
  const targets = new Set(monitorActionTargets().ids.map(String))
  if (!targets.size) return '暂无计划'

  const interval = Number(state.settings.full_scan_interval ?? 24)
  const now = Date.now() / 1000
  const upcoming = state.monitors
    .filter(row => targets.has(String(row.sec_uid)))
    .map(row => {
      const last = Number(row.last_full_scan_at || 0)
      if (last <= 0) return { row, remaining: 0 }
      if (interval <= 0) return null
      return { row, remaining: Math.max(0, last + interval * 3600 - now) }
    })
    .filter(Boolean)

  if (!upcoming.length) return '自动全量扫描已关闭'
  const next = upcoming.reduce((earliest, item) => item.remaining < earliest.remaining ? item : earliest)
  const minutes = Math.ceil(next.remaining / 60)
  const hours = Math.floor(minutes / 60)
  return `下次自动全量扫描：${hours} 小时 ${minutes % 60} 分后`
}
function renderFollowing() {
  const columnButton = `<div class="column-picker-anchor"><button type="button" class="icon-button column-picker-trigger" data-action="toggle-column-picker" aria-label="设置显示列" aria-haspopup="dialog" aria-expanded="${state.columnPickerOpen}" title="设置显示列"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="10" x2="10" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/></svg></button>${state.columnPickerOpen ? monitorColumnPicker() : ''}</div>`
  const validation = state.localValidate || { scanned: 0, total: 0, busy: false }
  const validateProgress = validation.total > 0 ? validation.scanned * 100 / validation.total : 0
  const validate = progressButton(validation.busy ? '正在校验…' : '校验本地文件', 'monitor-validate-local', validateProgress, validation.busy)
  const error = state.errors.monitor && state.monitors.length ? `<div class="notice error monitor-error" role="alert">刷新失败：${esc(state.errors.monitor)} ${button('重试', 'monitor-refresh', 'secondary')}</div>` : ''
  return `<div class="stack"><div class="monitor-entry-actions">${followingImportControl()}${button('手动添加', 'monitor-add-open', 'secondary')}<span class="spacer"></span>${columnButton}</div><div class="panel monitor-panel">${error}${monitorRows()}${monitorRowMenu()}${actionBar(`<span class="label" data-monitor-scope>${monitorActionScopeLabel()}</span><span class="spacer"></span>${validate}${button('全量扫描', 'monitor-full-scan', 'secondary', `title="${esc(fullScanScheduleTitle())}"`)}${button('检查更新', 'monitor-check')}${button('删除', 'monitor-delete', 'danger')}`)}</div></div>${followingImportModal()}${manualMonitorImportModal()}${monitorDeleteDialog()}`
}
function syncFollowingSelection() {
  const current = state.followingImport
  document.querySelectorAll('.following-list [data-action="following-toggle-row"]').forEach(button => {
    const selected = current.selected.has(button.dataset.id)
    AppUI.setRowSelected(button.closest('tr'), selected)
    if (!button.disabled) button.setAttribute('aria-label', selected ? '取消选择主播' : '选择主播')
  })
  const importable = current.items.filter(item => item && item.can_import)
  const all = importable.length > 0 && importable.every(item => current.selected.has(String(item.sec_uid)))
  const some = importable.some(item => current.selected.has(String(item.sec_uid)))
  const toggle = document.querySelector('[data-action="following-select-all"]')
  if (toggle) {
    toggle.classList.toggle('is-checked', all)
    toggle.setAttribute('aria-pressed', some && !all ? 'mixed' : String(all))
  }
  const count = document.querySelector('[data-following-selected]')
  if (count) count.textContent = `已选 ${current.selected.size} 人`
  const submit = document.querySelector('[data-action="following-import"]')
  if (submit) submit.disabled = !current.selected.size
}
function streamerCount(value) {
  const count = Number(value || 0)
  if (!Number.isFinite(count) || count <= 0) return '0'
  if (count >= 10000) return `${(count / 10000).toFixed(1).replace(/\.0$/, '')}万`
  return String(count)
}
function streamerDate(value) {
  const timestamp = Number(value || 0)
  if (!Number.isFinite(timestamp) || timestamp <= 0) return '未知时间'
  return new Date(timestamp * 1000).toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' })
}
function streamerPublishTime(value) {
  const timestamp = Number(value)
  if (!Number.isFinite(timestamp) || timestamp <= 0) return ''
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp))
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}小时前`
  if (seconds < 7 * 86400) return `${Math.floor(seconds / 86400)}天前`
  const date = new Date(timestamp * 1000)
  return date.getFullYear() === new Date().getFullYear()
    ? `${date.getMonth() + 1}月${date.getDate()}日`
    : `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`
}
function streamerFilteredWorks() {
  const query = String(state.streamer.search || '').trim().toLowerCase()
  const recentCutoff = state.streamer.date === 'recent' ? Math.floor(Date.now() / 1000) - 30 * 86400 : 0
  const hiddenOnly = !state.streamer.catalogue && state.streamer.subtab === 'hidden'
  const filtered = (state.streamer.works || []).filter(work => {
    const matchesQuery = !query || String(work.title || '').toLowerCase().includes(query)
    const matchesDate = !recentCutoff || Number(work.create_time || 0) >= recentCutoff
    const matchesHidden = !hiddenOnly || !!work.offline_label
    return matchesQuery && matchesDate && matchesHidden
  })
  return filtered // Local pages are ordered pinned-first in SQL; online lists keep API order.
}
function streamerMediaSource(work, index = 0) {
  const sources = Array.isArray(work && work.media_sources) ? work.media_sources : []
  return String(sources[index] || '')
}
function streamerMusicSources(work) {
  const sources = Array.isArray(work && work.music_sources)
    ? work.music_sources
    : (work && work.music_source ? [work.music_source] : [])
  return sources.map(value => String(value || '')).filter(Boolean)
}
function discardStreamerPreload(awemeId, entry) {
  if (!entry || streamerPreloadVideos.get(awemeId) !== entry) return
  streamerPreloadVideos.delete(awemeId)
  try {
    entry.video.pause()
    entry.video.removeAttribute('src')
    entry.video.load()
  } catch (_) {}
  entry.video.remove()
}
function clearStreamerPreloads() {
  streamerPreloadGeneration += 1
  for (const [awemeId, entry] of streamerPreloadVideos) discardStreamerPreload(awemeId, entry)
}
function preloadStreamerVideo(work) {
  const awemeId = String(work && work.aweme_id || '')
  const source = streamerMediaSource(work)
  if (!awemeId || !source) return
  const existing = streamerPreloadVideos.get(awemeId)
  if (existing && existing.source === source) return
  if (existing) discardStreamerPreload(awemeId, existing)
  const video = document.createElement('video')
  video.preload = 'auto'
  video.muted = true
  video.playsInline = true
  video.tabIndex = -1
  video.setAttribute('x5-playsinline', 'true')
  video.setAttribute('webkit-playsinline', 'true')
  video.style.cssText = 'position:fixed;left:-10000px;top:0;width:1px;height:1px;opacity:0;pointer-events:none;'
  const entry = { source, video }
  streamerPreloadVideos.set(awemeId, entry)
  video.addEventListener('error', () => discardStreamerPreload(awemeId, entry), { once: true })
  video.src = source
  document.body.appendChild(video)
  video.load()
}
function activateStreamerPreloadedVideo(root, work, modal) {
  if (modal.loading) return
  const awemeId = String(work && work.aweme_id || '')
  const source = streamerMediaSource(work, modal.mediaIndex)
  const entry = streamerPreloadVideos.get(awemeId)
  const player = root.querySelector('#streamer-media-video')
  if (!entry || entry.source !== source || !player) return
  streamerPreloadVideos.delete(awemeId)
  entry.video.removeAttribute('style')
  entry.video.removeAttribute('tabindex')
  entry.video.id = player.id
  entry.video.className = player.className
  entry.video.setAttribute('data-aweme-id', player.getAttribute('data-aweme-id') || awemeId)
  entry.video.muted = !!modal.muted
  entry.video.playsInline = true
  player.replaceWith(entry.video)
}
async function preloadStreamerNeighbours(session, modal) {
  const generation = ++streamerPreloadGeneration
  const visible = streamerFilteredWorks()
  const position = visible.findIndex(work => String(work.aweme_id || '') === modal.awemeId)
  if (position < 0) return
  // Keep the explicitly requested priority: next, previous, second next, second previous.
  const neighbours = [visible[position + 1], visible[position - 1], visible[position + 2], visible[position - 2]]
  const desired = new Set(neighbours.filter(work => work && work.media_type !== 'image').map(work => String(work.aweme_id || '')))
  for (const [awemeId, entry] of streamerPreloadVideos) {
    if (!desired.has(awemeId)) discardStreamerPreload(awemeId, entry)
  }
  for (const work of neighbours) {
    if (!work || work.media_type === 'image') continue
    if (state.page !== 'streamer' || state.streamer !== session || session.modal !== modal || generation !== streamerPreloadGeneration) return
    if (!(work.media_sources || []).some(Boolean)) {
      if (work.media_error) continue
      const index = session.works.indexOf(work)
      const detail = index >= 0 ? await ensureStreamerWorkDetail(index) : null
      if (state.page !== 'streamer' || state.streamer !== session || session.modal !== modal || generation !== streamerPreloadGeneration) return
      if (!detail || detail.media_error || !(detail.media_sources || []).some(Boolean)) continue
    }
    preloadStreamerVideo(work)
  }
}
function creatorView() {
  return (typeof window !== 'undefined' && window.CreatorView) || null
}
function streamerRelatedPanel() {
  if (!state.streamer.modal || !state.streamer.modal.relatedOpen) return ''
  const works = streamerFilteredWorks().slice(0, 8)
  const items = works.length ? works.map((work) => { const thumbSource = work.cover_source || (Array.isArray(work.media_sources) ? work.media_sources[0] : ''); const thumb = thumbSource ? `<img src="${esc(thumbSource)}" alt="" loading="lazy">` : '<span>暂无封面</span>'; return `<button type="button" class="streamer-related-item" data-action="streamer-open-work" data-sec-uid="${esc(state.streamer.secUid || '')}" data-index="${state.streamer.works.indexOf(work)}" data-aweme-id="${esc(work.aweme_id || '')}"><span class="streamer-related-thumb">${thumb}</span><span class="streamer-related-copy"><strong>${esc(work.title || '无标题')}</strong><small>${esc(streamerCount(work.like_count))} 赞 · ${esc(streamerDate(work.create_time))}</small></span></button>` }).join('') : '<span class="streamer-related-empty">暂无本地相关作品</span>'
  return `<aside class="streamer-related-panel" role="dialog" aria-label="相关推荐"><div class="streamer-related-head"><strong>相关推荐</strong><button type="button" class="streamer-related-close" data-action="streamer-related-toggle" aria-label="关闭相关推荐">${streamerRelatedCloseIcon}</button></div><div class="streamer-related-list">${items}</div></aside>`
}
// ── Player comment panel ─────────────────────────────────────────────
// Read live from Douyin like the web player: 10 comments per page, more as the
// list nears its end; replies fold under "展开N条回复" (3 first, then 10 at a
// time).  Nothing is saved.  The panel stays open while works change.
const STREAMER_REPLY_FIRST = 3
// The web player's own comment-like heart.
const streamerHeartIcon = '<svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path fill-rule="evenodd" clip-rule="evenodd" d="M7.646 4.168c-2.238 0-4.035 1.919-4.035 4.152l.002.13a.834.834 0 0 0-.001.073c.006.3.075.66.15.962.074.294.178.622.294.86.467 1.004 1.284 1.979 2.071 2.78a23.69 23.69 0 0 0 2.646 2.323l.015.012.012.01c.174.175.543.54 1.2.54h.019c.186 0 .63 0 1.028-.387l.033-.027c.033-.029.08-.067.14-.117l.003-.003c.436-.359 1.456-1.2 2.462-2.214.644-.646 1.312-1.396 1.822-2.17a7.94 7.94 0 0 0 .2-.318.84.84 0 0 0 .063-.13.956.956 0 0 1 .11-.214.835.835 0 0 0 .074-.144c.029-.073.05-.121.066-.154l.003-.007a.832.832 0 0 0 .147-.29c.125-.444.21-.835.219-1.316a.82.82 0 0 0-.002-.067 5.39 5.39 0 0 0 .002-.16c-.015-2.22-1.807-4.124-4.035-4.124-.845 0-1.667.262-2.316.789a4.029 4.029 0 0 0-2.392-.789zm7.076 4.153V8.424l-.002.07c0 .008 0 .022.002.039a3.065 3.065 0 0 1-.121.721 1.9 1.9 0 0 0-.078.144 3.297 3.297 0 0 0-.089.2c-.083.135-.137.24-.193.38a6.64 6.64 0 0 1-.124.195v.001c-.425.644-1.007 1.305-1.613 1.912l-.002.001a31.607 31.607 0 0 1-2.342 2.106l-.032.026-.12.1-.048-.046c-.05-.05-.119-.105-.152-.131l-.006-.005A22.003 22.003 0 0 1 7.32 11.96l-.003-.003c-.747-.76-1.408-1.577-1.753-2.323a3.149 3.149 0 0 1-.185-.555 3.468 3.468 0 0 1-.1-.553.964.964 0 0 0 0-.104V8.42a3.56 3.56 0 0 1-.001-.099c0-1.373 1.11-2.485 2.368-2.485.662 0 1.288.269 1.848.85a.833.833 0 0 0 1.282-.099c.33-.47.892-.751 1.578-.751 1.258 0 2.368 1.113 2.368 2.485z" fill="#fff" fill-opacity=".7" opacity=".9"/></svg>'
const streamerChevron = up => `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${up ? 'M6 15l6-6 6 6' : 'M6 9l6 6 6-6'}"/></svg>`
const streamerReplyArrow = '<svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M3 1.5l4 3.5-4 3.5z" fill="currentColor"/></svg>'
// Emoji codes in comment text ("[微笑]") are drawn as Douyin's pictures.
let streamerEmojis = null
let streamerEmojiRequest = null
function loadStreamerEmojis() {
  if (streamerEmojis || streamerEmojiRequest) return
  streamerEmojiRequest = call('comment_emojis').then(table => {
    streamerEmojis = table && typeof table === 'object' ? table : {}
    if (Object.keys(streamerEmojis).length) syncStreamerComments()
  }).finally(() => { streamerEmojiRequest = null })
}
function streamerCommentText(text) {
  const table = streamerEmojis || {}
  return esc(text).replace(/\[[^\[\]\s]{1,10}\]/g, code => {
    const url = table[code.replace(/&amp;/g, '&')]
    return url ? `<img class="cv-emoji" src="${esc(url)}" alt="${code}" title="${code}">` : code
  })
}
// A comment picture tries every mirror before it gives up (and then hides);
// links that failed once are skipped when the list is drawn again.
const streamerBrokenImages = new Set()
function streamerCommentImage(image, cid, index) {
  const mirrors = (image.mirrors && image.mirrors.length ? image.mirrors : [image.preview, image.full])
    .filter(url => url && !streamerBrokenImages.has(url))
  if (!mirrors.length) return ''
  const originals = [...new Set([image.full, ...mirrors])].filter(Boolean)
  return `<button type="button" data-action="streamer-comment-image" data-cid="${esc(cid)}" data-index="${index}" data-sources="${esc(JSON.stringify(originals))}" aria-label="放大评论图片"><img src="${esc(mirrors[0])}" data-mirrors="${esc(JSON.stringify(mirrors.slice(1)))}" alt="评论图片"></button>`
}
document.addEventListener('error', event => {
  const image = event.target
  if (!(image instanceof HTMLImageElement) || !image.closest('.cv-comments')) return
  if (image.getAttribute('src')) streamerBrokenImages.add(image.getAttribute('src'))
  let rest = []
  try { rest = JSON.parse(image.dataset.mirrors || '[]') } catch (_) {}
  if (rest.length) {
    image.dataset.mirrors = JSON.stringify(rest.slice(1))
    image.src = rest[0]
    return
  }
  const holder = image.closest('[data-action="streamer-comment-image"]') || image
  holder.hidden = true
}, true)
function openCommentImagePreview(sources, trigger = null) {
  document.querySelector('.image-overlay-close')?.click()
  const urls = [...new Set(sources.filter(Boolean))]
  if (!urls.length) return
  const returnFocus = trigger ? describeFocus(trigger) : state.imagePreviewReturnFocus
  const player = trigger?.closest('#streamer-modal')
  state.imagePreviewReturnFocus = null
  const overlay = document.createElement('div')
  overlay.className = 'image-overlay'
  overlay.setAttribute('role', 'dialog')
  overlay.setAttribute('aria-modal', 'true')
  overlay.setAttribute('aria-label', '评论图片预览')
  overlay.innerHTML = '<button type="button" class="image-overlay-close" aria-label="关闭图片预览">×</button><span class="image-overlay-message" role="status">正在加载图片…</span><img alt="评论图片预览" hidden>'
  const picture = overlay.querySelector('img')
  const message = overlay.querySelector('[role="status"]')
  let index = 0
  picture.addEventListener('load', () => { picture.hidden = false; message.hidden = true })
  picture.addEventListener('error', () => {
    if (++index < urls.length) picture.src = urls[index]
    else { picture.hidden = true; message.textContent = '图片加载失败，请关闭后重试'; message.hidden = false }
  })
  overlay.addEventListener('click', event => {
    if (event.target !== overlay && !event.target.closest('.image-overlay-close')) return
    overlay.remove()
    window.AppUI?.syncModal()
    const root = player?.isConnected ? player : byId('content')
    if (!restoreFocus(root, returnFocus)) focusFallback(root)
  })
  document.body.appendChild(overlay)
  window.AppUI?.syncModal()
  focusModal(overlay)
  picture.src = urls[0]
}
function streamerCommentAuthor(work) {
  return String(work && (work.author_sec_uid || (work.author_profile && work.author_profile.sec_uid)) || state.streamer.secUid || '')
}
function streamerCommentState(work) {
  const id = String(work && work.aweme_id || '')
  let comments = state.streamer.comments
  if (!comments || comments.awemeId !== id) {
    const hidden = !!(work && work.offline_label)
    // A hidden work cannot be read online: its saved comments are shown instead.
    comments = state.streamer.comments = { awemeId: id, items: [], cursor: 0, hasMore: true, loading: false,
      error: '', total: Number(work && work.comment_count || 0), hidden, replies: {}, tab: 'all',
      author: { items: [], cursor: 0, hasMore: true, loading: false, error: '' } }
  }
  return comments
}
// The 作者 tab: each comment the author wrote or replied under, with only the
// author's replies below it.  Saved threads first, then threads read online;
// a comment also read online shows the online copy (its links are fresh).
function streamerAuthorThreads(comments) {
  const threads = comments.author.items.map(item => ({ ...item, author_replies: (item.author_replies || []).slice() }))
  const byCid = new Map(threads.map(thread => [thread.cid, thread]))
  comments.items.forEach(item => {
    const replies = ((comments.replies[item.cid] || {}).items || []).filter(reply => reply.is_author)
    let thread = byCid.get(item.cid)
    if (!thread && !item.is_author && !replies.length) return
    if (thread) Object.assign(thread, item, { author_replies: thread.author_replies, reply_total: 0 })
    else { thread = { ...item, author_replies: [] }; byCid.set(item.cid, thread); threads.push(thread) }
    replies.forEach(reply => {
      const at = thread.author_replies.findIndex(saved => saved.cid === reply.cid)
      if (at >= 0) thread.author_replies[at] = reply
      else thread.author_replies.push(reply)
    })
  })
  return threads
}
function streamerAuthorListHtml(comments) {
  const author = comments.author
  const threads = streamerAuthorThreads(comments)
  const html = threads.map(thread => `<div class="cv-comment-thread">${streamerCommentHtml({ ...thread, reply_total: 0 })}` +
    (thread.author_replies.length ? `<div class="cv-replies">${thread.author_replies.map(reply => streamerCommentHtml(reply, true)).join('')}</div>` : '') + '</div>').join('')
  const footer = author.loading ? '<div class="cv-comments-foot">加载中</div>'
    : author.error ? `<div class="cv-comments-foot">${esc(author.error)} <button type="button" data-action="streamer-comments-retry">重试</button></div>`
      : author.hasMore ? '' : `<div class="cv-comments-foot">${threads.length ? '暂时没有更多评论' : '暂无作者评论'}</div>`
  return html + footer
}
function streamerCommentHtml(comment, reply = false) {
  const avatar = comment.avatar ? `<img src="${esc(comment.avatar)}" alt="" loading="lazy">` : `<span>${esc(String(comment.nickname || '').slice(0, 1))}</span>`
  const name = `<span class="cv-comment-name">${esc(comment.nickname)}</span>${comment.is_author ? '<span class="cv-comment-tag">作者</span>' : ''}` +
    (reply && comment.reply_to ? `<span class="cv-comment-reply-to">${streamerReplyArrow}</span><span class="cv-comment-name">${esc(comment.reply_to)}</span>` : '')
  const stickers = (comment.sticker_mirrors && comment.sticker_mirrors.length ? comment.sticker_mirrors : [comment.sticker])
    .filter(url => url && !streamerBrokenImages.has(url))
  const media = (stickers.length ? `<img class="cv-comment-sticker" src="${esc(stickers[0])}" data-mirrors="${esc(JSON.stringify(stickers.slice(1)))}" alt="表情" loading="lazy">` : '') +
    ((comment.images || []).length ? `<div class="cv-comment-images">${comment.images.slice(0, 9).map((image, index) => streamerCommentImage(image, comment.cid, index)).join('')}</div>` : '')
  const badge = comment.author_liked ? '<span class="cv-comment-badge">作者赞过</span>' : ''
  const meta = [streamerPublishTime(comment.create_time), comment.ip_label].filter(Boolean).join('·')
  return `<div class="cv-comment${reply ? ' is-reply' : ''}" data-cid="${esc(comment.cid)}"><span class="cv-comment-avatar">${avatar}</span><div class="cv-comment-body">` +
    `<div class="cv-comment-head">${name}</div>` +
    (comment.text || badge ? `<p class="cv-comment-text">${streamerCommentText(comment.text || '')}${badge}</p>` : '') +
    media + (meta ? `<div class="cv-comment-meta">${esc(meta)}</div>` : '') +
    `<div class="cv-comment-actions"><span class="cv-comment-like">${streamerHeartIcon}${esc(comment.digg_count ? streamerCount(comment.digg_count) : '0')}</span></div></div></div>`
}
function streamerRepliesHtml(comment, replies) {
  const total = Number(comment.reply_total || 0)
  if (!total) return ''
  const open = !!(replies && replies.open)
  const thread = open ? replies.items.map(reply => streamerCommentHtml(reply, true)).join('') : ''
  const loaded = open ? replies.items.length : 0
  const cid = esc(comment.cid)
  let control
  if (replies && replies.loading) control = '<span class="cv-reply-toggle is-loading"><span class="cv-reply-line"></span>加载中</span>'
  else if (replies && replies.error) control = `<button type="button" class="cv-reply-toggle is-error" data-action="streamer-replies-more" data-cid="${cid}" title="${esc(replies.error)}"><span class="cv-reply-line"></span>${esc(replies.error)}<span class="cv-reply-retry">重试</span></button>`
  else if (open && !replies.hasMore) control = `<button type="button" class="cv-reply-toggle" data-action="streamer-replies-collapse" data-cid="${cid}"><span class="cv-reply-line"></span>收起${streamerChevron(true)}</button>`
  else control = `<button type="button" class="cv-reply-toggle" data-action="streamer-replies-more" data-cid="${cid}"><span class="cv-reply-line"></span>展开${Math.max(1, total - loaded)}条回复${streamerChevron(false)}</button>`
  return `<div class="cv-replies">${thread}${control}</div>`
}
function streamerCommentsListHtml() {
  const comments = streamerCommentState(streamerCurrentWork())
  if (comments.tab === 'author') return streamerAuthorListHtml(comments)
  // A hidden work shows its saved comments.
  const note = comments.hidden ? '<div class="cv-comments-note">作品已隐藏</div>' : ''
  const items = comments.items.map(comment => `<div class="cv-comment-thread">${streamerCommentHtml(comment)}${streamerRepliesHtml(comment, comments.replies[comment.cid])}</div>`).join('')
  const footer = comments.loading ? '<div class="cv-comments-foot">加载中</div>'
    : comments.error ? `<div class="cv-comments-foot">${esc(comments.error)} <button type="button" data-action="streamer-comments-retry">重试</button></div>`
      : !comments.hasMore ? `<div class="cv-comments-foot">${comments.items.length ? '暂时没有更多评论' : '暂无评论'}</div>` : ''
  return note + items + footer
}
function streamerCommentsTotal(comments) {
  if (comments && comments.tab === 'author') return `(${streamerAuthorThreads(comments).length})`
  return `(${comments && comments.total ? comments.total : 0})`
}
function streamerCommentsTabsHtml(comments) {
  const tab = (key, label) => `<button type="button" class="cv-comments-tab${comments.tab === key ? ' is-active' : ''}" role="tab" aria-selected="${comments.tab === key}" data-action="streamer-comments-tab" data-tab="${key}">${label}</button>`
  return tab('all', '评论') + tab('author', '作者')
}
function setStreamerCommentsTab(key) {
  const comments = state.streamer.comments
  if (!comments || comments.tab === key) return
  comments.tab = key === 'author' ? 'author' : 'all'
  syncStreamerComments()
  loadStreamerComments()
}
function streamerCommentsPanel() {
  const comments = streamerCommentState(streamerCurrentWork())
  return `<aside class="cv-comments" aria-label="评论"><div class="cv-comments-tabs" role="tablist">${streamerCommentsTabsHtml(comments)}<span class="cv-spacer"></span>` +
    `<button type="button" class="cv-comments-close" data-action="streamer-comments-toggle" aria-label="关闭评论">${streamerRelatedCloseIcon}</button></div>` +
    `<div class="cv-comments-scroll"><div class="cv-comments-title"><span data-comments-label>${comments.tab === 'author' ? '作者评论' : '全部评论'}</span><span data-comments-total>${esc(streamerCommentsTotal(comments))}</span></div>` +
    `<div class="cv-comments-list">${streamerCommentsListHtml()}</div></div></aside>`
}
function syncStreamerComments() {
  const panel = document.querySelector('#streamer-modal .cv-comments')
  if (!panel) return
  const comments = state.streamer.comments
  const list = panel.querySelector('.cv-comments-list')
  if (list) list.innerHTML = streamerCommentsListHtml()
  const total = panel.querySelector('[data-comments-total]')
  if (total) total.textContent = streamerCommentsTotal(comments)
  if (!comments) return
  const label = panel.querySelector('[data-comments-label]')
  if (label) label.textContent = comments.tab === 'author' ? '作者评论' : '全部评论'
  panel.querySelectorAll('.cv-comments-tab').forEach(node => {
    const active = node.dataset.tab === comments.tab
    node.classList.toggle('is-active', active)
    node.setAttribute('aria-selected', String(active))
  })
  // Keep reading while the first pages do not fill the panel yet.
  const shown = comments.tab === 'author' ? comments.author : comments
  const scroller = panel.querySelector('.cv-comments-scroll')
  if (scroller && shown.hasMore && !shown.loading && !shown.error
      && scroller.scrollHeight - scroller.clientHeight < 200) loadStreamerComments(true)
}
async function loadStreamerComments(more = false) {
  if (!state.streamer.commentsOpen || !state.streamer.modal) return
  loadStreamerEmojis()
  const work = streamerCurrentWork()
  if (!work) return
  const comments = streamerCommentState(work)
  if (comments.tab === 'author') return loadStreamerAuthorComments(comments, work, more)
  if (comments.loading || !comments.hasMore || (!more && comments.items.length)) return
  await fetchStreamerCommentPage(comments, work)
}
// The next page of the 评论 list (online, or saved for a hidden work); false
// when it could not be read.
async function fetchStreamerCommentPage(comments, work) {
  comments.loading = true
  comments.error = ''
  syncStreamerComments()
  const result = await call('work_comments', comments.awemeId, comments.cursor, streamerCommentAuthor(work), '', 0, comments.hidden)
  if (state.streamer.comments !== comments) return false
  comments.loading = false
  if (!result || result.error) {
    comments.error = '获取失败'
  } else {
    const seen = new Set(comments.items.map(item => item.cid))
    ;(result.comments || []).forEach(item => { if (!seen.has(item.cid)) { seen.add(item.cid); comments.items.push(item) } })
    comments.cursor = Number(result.cursor || 0)
    comments.hasMore = !!result.has_more
    if (comments.hidden || Number(result.total || 0)) comments.total = Number(result.total || 0)
  }
  syncStreamerComments()
  return !comments.error
}
async function loadStreamerAuthorComments(comments, work, more) {
  if (!comments.hidden) return walkStreamerAuthorComments(comments, work)
  const author = comments.author
  if (author.loading || !author.hasMore || (!more && author.items.length)) return
  author.loading = true
  author.error = ''
  syncStreamerComments()
  const result = await call('work_comments', comments.awemeId, author.cursor, streamerCommentAuthor(work), '', 0, true, true)
  if (state.streamer.comments !== comments) return
  author.loading = false
  if (!result || result.error) {
    author.error = '获取失败'
  } else {
    const seen = new Set(author.items.map(item => item.cid))
    ;(result.comments || []).forEach(item => { if (!seen.has(item.cid)) { seen.add(item.cid); author.items.push(item) } })
    author.cursor = Number(result.cursor || 0)
    author.hasMore = !!result.has_more
  }
  syncStreamerComments()
}
async function loadStreamerReplies(cid) {
  const work = streamerCurrentWork()
  const comments = streamerCommentState(work)
  const comment = comments.items.find(item => item.cid === cid)
  if (!comment) return
  const replies = comments.replies[cid] || (comments.replies[cid] = { items: [], cursor: 0, hasMore: true, loading: false, error: '', open: false })
  if (replies.loading) return
  // Replies read before are shown again first; the next click reads more.
  if (!replies.open && replies.items.length) { replies.open = true; syncStreamerComments(); return }
  replies.open = true
  await fetchStreamerReplyPage(comments, work, comment, replies.items.length ? 10 : STREAMER_REPLY_FIRST)
}
async function fetchStreamerReplyPage(comments, work, comment, count) {
  const replies = comments.replies[comment.cid] || (comments.replies[comment.cid] = { items: [], cursor: 0, hasMore: true, loading: false, error: '', open: false })
  replies.loading = true
  replies.error = ''
  syncStreamerComments()
  const result = await call('work_comments', comments.awemeId, replies.cursor, streamerCommentAuthor(work), comment.cid, count, comments.hidden)
  if (state.streamer.comments !== comments) return
  replies.loading = false
  if (!result || result.error) {
    replies.error = (result && result.error) || '回复读取失败'
  } else {
    const before = replies.items.length
    const seen = new Set(replies.items.map(item => item.cid))
    ;(result.comments || []).forEach(item => { if (!seen.has(item.cid)) { seen.add(item.cid); replies.items.push(item) } })
    replies.cursor = Number(result.cursor || 0)
    // A page that adds nothing ends the replies, so the 作者 walk cannot loop.
    replies.hasMore = !!result.has_more && replies.items.length > before && replies.items.length < Number(comment.reply_total || 0)
  }
  syncStreamerComments()
}
// The 作者 tab of a work that is online: Douyin cannot filter comments by
// author, so every comment page is read and, under each comment with
// replies, every reply page; the author's threads show up as they are found.
// It pauses while another tab is shown and goes on when 作者 is back.
async function walkStreamerAuthorComments(comments, work) {
  const author = comments.author
  if (author.loading || !author.hasMore) return
  author.loading = true
  author.error = ''
  syncStreamerComments()
  const waitingReplies = item => {
    const replies = comments.replies[item.cid]
    return Number(item.reply_total || 0) > 0 && (!replies || (replies.hasMore && !replies.error))
  }
  while (state.streamer.comments === comments && comments.tab === 'author') {
    const busy = comments.loading || Object.values(comments.replies).some(replies => replies.loading)
    const pending = !busy && comments.items.find(waitingReplies)
    if (busy) await new Promise(resolve => setTimeout(resolve, 300))
    else if (pending) await fetchStreamerReplyPage(comments, work, pending, 10)    // one thread's failure is skipped
    else if (comments.hasMore && !comments.error) { if (!await fetchStreamerCommentPage(comments, work)) break }
    else { author.hasMore = !!comments.error; break }
  }
  if (state.streamer.comments !== comments) return
  author.loading = false
  if (comments.error) author.error = '获取失败'
  syncStreamerComments()
}
// The web player opens its panel without an animation; here it slides in
// from the right while the picture gives way, and slides back out.
const STREAMER_PANEL_MS = 260
// Like / collect in the player: a local mark only, with Douyin-like colours
// and a pop; the app cannot like or collect on Douyin itself.
let streamerReactionNoted = false
function streamerReaction(work) {
  const marks = state.streamer.reactions || (state.streamer.reactions = {})
  return marks[String(work && work.aweme_id || '')] || {}
}
function toggleStreamerReaction(kind, node) {
  const work = streamerCurrentWork()
  if (!work) return
  const marks = state.streamer.reactions || (state.streamer.reactions = {})
  const key = String(work.aweme_id || '')
  const mark = marks[key] = { ...(marks[key] || {}) }
  mark[kind] = !mark[kind]
  node.classList.toggle('is-on', mark[kind])
  node.setAttribute('aria-pressed', String(mark[kind]))
  node.classList.remove('is-popping')
  void node.offsetWidth
  if (mark[kind]) {
    node.classList.add('is-popping')
    // No second hop right after the pop while the mouse is still on it.
    node.classList.add('no-hop')
    node.addEventListener('mouseleave', () => node.classList.remove('no-hop'), { once: true })
    setTimeout(() => node.classList.remove('is-popping'), 480)
  }
  if (mark[kind] && !streamerReactionNoted) {
    streamerReactionNoted = true
    showToast('点赞和收藏只在本地显示，不会同步到抖音')
  }
}
function toggleStreamerComments() {
  if (!state.streamer.modal) return
  state.streamer.commentsOpen = !state.streamer.commentsOpen
  if (state.streamer.commentsOpen) state.streamer.modal.relatedOpen = false
  const modalNode = byId('streamer-modal')
  const animate = !!modalNode && streamerMotionAllowed()
  if (animate) modalNode.classList.add('is-panel-moving')
  const leaving = !state.streamer.commentsOpen && modalNode && modalNode.querySelector('.cv-comments')
  if (leaving && animate && typeof leaving.animate === 'function') {
    // Keep the old panel on screen while it slides out.
    const ghost = leaving.cloneNode(true)
    ghost.classList.add('is-leaving')
    ghost.setAttribute('aria-hidden', 'true')
    modalNode.appendChild(ghost)
    ghost.animate([{ transform: 'translateX(0)' }, { transform: 'translateX(100%)' }],
      { duration: STREAMER_PANEL_MS, easing: STREAMER_SLIDE_EASING, fill: 'forwards' })
      .finished.catch(() => {}).finally(() => ghost.remove())
    setTimeout(() => ghost.remove(), STREAMER_PANEL_MS + 200)
  }
  renderStreamerOverlay()
  const panel = state.streamer.commentsOpen && byId('streamer-modal') && byId('streamer-modal').querySelector('.cv-comments')
  if (panel && animate && typeof panel.animate === 'function') {
    panel.animate([{ transform: 'translateX(100%)' }, { transform: 'translateX(0)' }],
      { duration: STREAMER_PANEL_MS, easing: STREAMER_SLIDE_EASING })
  }
  if (animate) setTimeout(() => { const node = byId('streamer-modal'); if (node) node.classList.remove('is-panel-moving') }, STREAMER_PANEL_MS + 40)
}
document.addEventListener('scroll', event => {
  const scroller = event.target
  if (!(scroller instanceof Element) || !scroller.classList.contains('cv-comments-scroll')) return
  if (scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 300) loadStreamerComments(true)
}, true)
function renderStreamerHome() {
  const view = creatorView()
  return view && view.renderHome ? view.renderHome() : ''
}

function streamerCurrentWork() {
  const modal = state.streamer.modal
  if (!modal) return null
  return state.streamer.works[Number(modal.workIndex)] || null
}
function syncStreamerPlayerControls() {
  const modalNode = byId('streamer-modal')
  const modal = state.streamer.modal
  if (!modalNode || !modal) return
  modalNode.classList.toggle('is-clear-screen', !!modal.clearScreen)
  // The media element is the only thing that truly knows whether it is playing;
  // `modal.playing` is just a mirror of it.  Reading it back here keeps the two
  // glyphs honest even if some later path starts or stops playback without
  // going through the play/pause handlers.
  const player = byId('streamer-media-video') || byId('streamer-media-audio')
  // A gallery's play state is its own clock, not whether the music is audible.
  const gallery = streamerCurrentWork()?.media_type === 'image'
  const playing = gallery ? streamerGalleryPlaying() : player ? !player.paused : !!modal.playing
  modal.playing = playing
  // Live photo clips pause and resume with the gallery.
  if (gallery && typeof modalNode.querySelectorAll === 'function') {
    const view = creatorView()
    modalNode.querySelectorAll('video.streamer-live-clip').forEach(clip => {
      if (!playing || !modal.mediaStarted) clip.pause()
      else if (view && view.playLiveClip) view.playLiveClip(clip)
    })
  }
  const play = modalNode.querySelector('.cv-play')
  if (play) play.dataset.state = playing ? 'play' : 'pause'
  // The big centre button only offers "play"; it disappears while playing.
  const start = modalNode.querySelector('.cv-start')
  if (start) start.hidden = playing
  const volume = modalNode.querySelector('.cv-volume')
  if (volume) volume.dataset.state = modal.muted ? 'muted' : 'normal'
  const clearSwitch = modalNode.querySelector('[data-action="streamer-clear-screen"]')
  if (clearSwitch) clearSwitch.setAttribute('aria-pressed', String(!!modal.clearScreen))
  const autoSwitch = modalNode.querySelector('[data-action="streamer-auto-play"]')
  if (autoSwitch) autoSwitch.setAttribute('aria-pressed', String(!!modal.autoPlay))
  syncStreamerVolumeControl()
  // The up / down arrows switch works; at either end the arrow is greyed out.
  const { list, position } = streamerWorkPosition()
  const previousWork = modalNode.querySelector('[data-action="streamer-work-prev"]')
  const nextWork = modalNode.querySelector('[data-action="streamer-work-next"]')
  if (previousWork) previousWork.disabled = position <= 0
  if (nextWork) nextWork.disabled = position < 0 || (position >= list.length - 1 && !state.streamer.hasMore)
}
// The open work's place among the works shown in the grid; a work the current
// search or filters leave out is placed in the full list instead.
function streamerWorkPosition() {
  const modal = state.streamer.modal
  const id = modal ? modal.awemeId : ''
  const visible = streamerFilteredWorks()
  const position = visible.findIndex(work => String(work.aweme_id) === id)
  if (position >= 0) return { list: visible, position }
  const all = state.streamer.works || []
  return { list: all, position: all.findIndex(work => String(work.aweme_id) === id) }
}
function updateStreamerPictureProgress() {
  const modal = state.streamer.modal
  const work = streamerCurrentWork()
  const progress = document.querySelector('[data-streamer-picture-progress]')
  if (!modal || !work || work.media_type !== 'image' || !progress) return
  const segments = [...progress.querySelectorAll('[data-streamer-segment]')]
  if (!segments.length) return
  const pictureProgress = Math.max(0, Math.min(100, Number(modal.pictureProgress) || 0))
  const current = Math.min(Math.max(0, Number(modal.mediaIndex) || 0), segments.length - 1)
  progress.dataset.index = String(current)
  segments.forEach((segment, index) => {
    const played = segment.querySelector('.cv-progress-played')
    if (!played) return
    const width = index < current ? 100 : (index === current ? pictureProgress : 0)
    played.style.width = `${width}%`
  })
}
// A gallery runs on its own clock: every picture stays up for a fixed time
// (a live photo at least for one pass of its clip) while the soundtrack just
// loops in the background.  Paging never seeks the music.
const STREAMER_PICTURE_SECONDS = 4
let streamerGalleryTimer = null
let streamerGalleryTickAt = 0
function streamerPictureSeconds() {
  const clip = typeof document.querySelector === 'function' && document.querySelector('#streamer-modal video.streamer-live-clip')
  const clipSeconds = Number(clip && clip.duration)
  return Math.max(STREAMER_PICTURE_SECONDS, Number.isFinite(clipSeconds) ? clipSeconds : 0)
}
function setStreamerPicture(index, local = 0) {
  const modal = state.streamer.modal
  if (!modal) return
  const ratio = Math.max(0, Math.min(1, Number(local) || 0))
  modal.mediaIndex = index
  modal.pictureProgress = ratio * 100
  syncStreamerGallery()
  // Measured after the sync, once this picture's own clip is on screen.
  modal.pictureElapsed = ratio * streamerPictureSeconds()
}
function advanceStreamerGallery(seconds) {
  const modal = state.streamer.modal
  const work = streamerCurrentWork()
  if (!modal || !work || work.media_type !== 'image' || modal.loading || modal.galleryPaused || modal.advancing) return
  const total = Array.isArray(work.media_sources) ? work.media_sources.length : 0
  if (!total) return
  const pictureSeconds = streamerPictureSeconds()
  modal.pictureElapsed = Number(modal.pictureElapsed || 0) + Math.max(0, Number(seconds) || 0)
  if (modal.pictureElapsed < pictureSeconds) {
    modal.pictureProgress = modal.pictureElapsed / pictureSeconds * 100
    updateStreamerPictureProgress()
    return
  }
  const current = Math.min(Math.max(0, Number(modal.mediaIndex) || 0), total - 1)
  if (current < total - 1) { setStreamerPicture(current + 1); return }
  const visible = streamerFilteredWorks()
  const position = visible.findIndex(item => String(item.aweme_id || '') === String(modal.awemeId || ''))
  if (modal.autoPlay && position >= 0 && (position < visible.length - 1 || state.streamer.hasMore)) {
    modal.advancing = true
    moveStreamerWork(1).catch(() => {})
    return
  }
  setStreamerPicture(0)
}
function startStreamerGalleryClock() {
  if (streamerGalleryTimer || typeof window.setInterval !== 'function') return
  streamerGalleryTickAt = Date.now()
  streamerGalleryTimer = window.setInterval(() => {
    const now = Date.now()
    const seconds = (now - streamerGalleryTickAt) / 1000
    streamerGalleryTickAt = now
    const work = streamerCurrentWork()
    if (state.page !== 'streamer' || !work || work.media_type !== 'image') {
      window.clearInterval(streamerGalleryTimer)
      streamerGalleryTimer = null
      return
    }
    if (!document.hidden) advanceStreamerGallery(seconds)
  }, 100)
}
function toggleStreamerGallery() {
  const modal = state.streamer.modal
  if (!modal) return
  modal.galleryPaused = !modal.galleryPaused
  if (!modal.galleryPaused) modal.mediaStarted = true
  const soundtrack = streamerGallerySoundtrack()
  if (soundtrack) {
    if (modal.galleryPaused) soundtrack.pause()
    else soundtrack.play().catch(() => {})
  }
  syncStreamerPlayerControls()
}
// A gallery pauses its own clock (and with it the music and live clips); a
// video pauses the element itself.
function toggleStreamerPlayback() {
  if (streamerCurrentWork()?.media_type === 'image') { toggleStreamerGallery(); return }
  const player = byId('streamer-media-video')
  if (!player) return
  if (player.paused) player.play().catch(() => showToast('媒体播放失败'))
  else player.pause()
}
// A gallery without a soundtrack has an empty player; its live clips just loop.
function streamerGallerySoundtrack() {
  const player = byId('streamer-media-audio')
  return player && player.getAttribute('src') ? player : null
}
function streamerGalleryPlaying() {
  const modal = state.streamer.modal
  return !(modal && modal.galleryPaused)
}
function syncStreamerGallery() {
  const modalNode = byId('streamer-modal')
  const modal = state.streamer.modal
  const work = streamerCurrentWork()
  if (!modalNode || !modal || !work || work.media_type !== 'image') return
  const sources = Array.isArray(work.media_sources) ? work.media_sources : []
  const current = Math.min(Math.max(0, Number(modal.mediaIndex) || 0), Math.max(0, sources.length - 1))
  const source = sources[current] || ''
  if (typeof modalNode.querySelectorAll !== 'function') {
    updateStreamerPictureProgress(byId('streamer-media-audio'))
    return
  }
  const shown = modalNode.dataset.shownIndex === undefined ? current : Number(modalNode.dataset.shownIndex)
  modalNode.dataset.shownIndex = String(current)
  // Looping from the last picture back to the first still moves forward.
  const pictureDirection = shown === current || !source ? 0
    : shown === sources.length - 1 && current === 0 ? 1 : Math.sign(current - shown)
  if (pictureDirection) slideStreamerPicture(modalNode, pictureDirection, 'out')
  modalNode.querySelectorAll('img.cv-picture, .cv-backdrop img').forEach(image => {
    if (source && image.getAttribute('src') !== source) {
      image.setAttribute('src', source)
      if (image.classList.contains('cv-picture')) image.alt = String(work.title || '')
    }
  })
  const previous = modalNode.querySelector('[data-action="streamer-media-prev"]')
  const next = modalNode.querySelector('[data-action="streamer-media-next"]')
  if (previous) previous.disabled = current <= 0
  if (next) next.disabled = current >= sources.length - 1
  const view = creatorView()
  if (view && view.syncLiveClip) view.syncLiveClip(modalNode, work, current, streamerGalleryPlaying())
  if (pictureDirection) slideStreamerPicture(modalNode, pictureDirection, 'in')
  if (view && view.rebuildPictureSegments) {
    view.rebuildPictureSegments(modalNode, sources.length, current, Number(modal.pictureProgress || 0))
  }
  updateStreamerPictureProgress(byId('streamer-media-audio'))
}
function renderStreamerModal() {
  const view = creatorView()
  return view && view.renderModal ? view.renderModal() : ''
}
function bindStreamerPlayerChrome(modalNode) {
  const view = creatorView()
  if (view && view.bindPlayerChrome) view.bindPlayerChrome(modalNode)
}
// Switching works slides like a feed: the old picture leaves upwards (or
// downwards) while the new one comes in from the other side.  A work is
// rendered twice (loading, then with its media); the second render resumes
// the same slide instead of restarting it.
const STREAMER_SLIDE_MS = 320
const STREAMER_SLIDE_EASING = 'cubic-bezier(.2, .8, .2, 1)'
let streamerSlidePending = 0
let streamerSlide = null
function streamerMotionAllowed() { return AppPreferences.motionAllowed() }
function slideOutStreamerStage(existing, direction) {
  const stage = existing && existing.querySelector('.cv-stage')
  if (!stage || typeof stage.animate !== 'function') return
  stage.querySelectorAll('video, audio').forEach(media => { try { media.pause() } catch (_) {} })
  stage.querySelectorAll('[id]').forEach(node => node.removeAttribute('id'))
  const layer = document.createElement('div')
  layer.className = 'cv-slide-layer'
  layer.setAttribute('aria-hidden', 'true')
  // Only over the picture: an open comment panel stays on top, untouched.
  const panel = existing.querySelector('.cv-comments')
  if (panel) layer.style.right = `${panel.getBoundingClientRect().width}px`
  layer.appendChild(stage)
  document.body.appendChild(layer)
  layer.animate([{ transform: 'translateY(0)' }, { transform: `translateY(${-direction * 100}%)` }],
    { duration: STREAMER_SLIDE_MS, easing: STREAMER_SLIDE_EASING, fill: 'forwards' })
    .finished.catch(() => {}).finally(() => layer.remove())
  // Animations stall in a hidden window; never leave the old picture behind.
  setTimeout(() => layer.remove(), STREAMER_SLIDE_MS + 200)
}
function slideInStreamerStage(node, awemeId) {
  const slide = streamerSlide
  const stage = node.querySelector('.cv-stage')
  if (!slide || slide.awemeId !== awemeId || !stage || typeof stage.animate !== 'function') return
  const elapsed = performance.now() - slide.start
  if (elapsed >= STREAMER_SLIDE_MS) { streamerSlide = null; return }
  const animation = stage.animate([{ transform: `translateY(${slide.direction * 100}%)` }, { transform: 'translateY(0)' }],
    { duration: STREAMER_SLIDE_MS, easing: STREAMER_SLIDE_EASING })
  animation.currentTime = elapsed
}
// Gallery pictures slide sideways: a copy of the old picture leaves while
// the new one comes in.
function slideStreamerPicture(modalNode, direction, phase) {
  if (!streamerMotionAllowed()) return null
  const stage = modalNode.querySelector('.cv-stage')
  const visible = stage && [...stage.querySelectorAll('img.cv-picture, video.streamer-live-clip')]
    .find(node => node.style.display !== 'none')
  if (!visible || typeof visible.animate !== 'function') return null
  const options = { duration: 300, easing: STREAMER_SLIDE_EASING }
  if (phase === 'in') {
    visible.animate([{ transform: `translateX(${direction * 100}%)` }, { transform: 'translateX(0)' }], options)
    return null
  }
  if (visible.tagName !== 'IMG') return null
  const ghost = visible.cloneNode(false)
  ghost.className = 'cv-picture-ghost'
  ghost.removeAttribute('style')
  ghost.setAttribute('aria-hidden', 'true')
  visible.insertAdjacentElement('beforebegin', ghost)
  ghost.animate([{ transform: 'translateX(0)' }, { transform: `translateX(${-direction * 100}%)` }], { ...options, fill: 'forwards' })
    .finished.catch(() => {}).finally(() => ghost.remove())
  setTimeout(() => ghost.remove(), options.duration + 200)
  return ghost
}
function renderStreamerOverlay() {
  const existing = byId('streamer-modal')
  const current = state.streamer.modal
  const work = streamerCurrentWork()
  const isImageGallery = !!work && work.media_type === 'image'
  const key = state.page === 'streamer' && current
    ? JSON.stringify([current.awemeId, isImageGallery ? 'image' : current.mediaIndex, current.loading, current.error, isImageGallery ? streamerMusicSources(work).join('\u0001') : streamerMediaSource(work, current.mediaIndex)]) : ''
  if (existing && existing.dataset.renderKey === key) {
    syncStreamerPlayerControls()
    if (isImageGallery) syncStreamerGallery()
    const related = existing.querySelector('.streamer-related-panel')
    if (related && !current.relatedOpen) related.remove()
    if (!related && current.relatedOpen) existing.insertAdjacentHTML('beforeend', streamerRelatedPanel())
    // The comment panel opens and closes in place, without an animation (like the web player).
    const panel = existing.querySelector('.cv-comments')
    const commentsOpen = !!state.streamer.commentsOpen
    existing.classList.toggle('has-comments', commentsOpen)
    const toggle = existing.querySelector('.cv-rail [data-action="streamer-comments-toggle"]')
    if (toggle) { toggle.classList.toggle('is-active', commentsOpen); toggle.setAttribute('aria-pressed', String(commentsOpen)) }
    if (panel && !commentsOpen) panel.remove()
    if (!panel && commentsOpen) { existing.insertAdjacentHTML('beforeend', streamerCommentsPanel()); loadStreamerComments() }
    return
  }
  if (existing && streamerSlidePending && current && existing.dataset.awemeId && existing.dataset.awemeId !== current.awemeId && streamerMotionAllowed()) {
    streamerSlide = { awemeId: current.awemeId, direction: streamerSlidePending, start: performance.now() }
    slideOutStreamerStage(existing, streamerSlidePending)
  }
  streamerSlidePending = 0
  if (existing && existing.parentElement) existing.parentElement.removeChild(existing)
  const modal = state.page === 'streamer' ? renderStreamerModal() : ''
  document.body.classList.toggle('streamer-modal-open', !!modal)
  if (!window.AppUI) byId('app').toggleAttribute('inert', !!modal)
  if (!modal) { clearStreamerPreloads(); return }
  document.body.insertAdjacentHTML('beforeend', modal)
  const node = byId('streamer-modal')
  node.dataset.renderKey = key
  node.dataset.awemeId = current.awemeId
  slideInStreamerStage(node, current.awemeId)
  if (!isImageGallery) activateStreamerPreloadedVideo(node, work, current)
  bindStreamerPlayerChrome(node)
  syncStreamerPlayerControls()
  if (isImageGallery) { syncStreamerGallery(); startStreamerGalleryClock() }
  if (state.streamer.commentsOpen) loadStreamerComments()
  const player = byId('streamer-media-video') || byId('streamer-media-audio')
  if (player) {
    try { player.volume = streamerVolumeValue(current.volume) } catch (_) {}
    try { player.muted = !!current.muted } catch (_) {}
    player.addEventListener('timeupdate', () => {
      const current = Number(player.currentTime || 0); const duration = Number(player.duration || 0)
      if (!state.streamer.modal || !player.isConnected) return
      state.streamer.modal.progress = duration > 0 ? Math.round(current / duration * 1000) : 0
      const played = document.querySelector('[data-streamer-progress-bar] .cv-progress-played')
      if (played && work?.media_type !== 'image') played.style.width = `${state.streamer.modal.progress / 10}%`
    })
    player.addEventListener('ended', () => {
      const modal = state.streamer.modal
      const work = streamerCurrentWork()
      // A gallery's soundtrack loops; its own clock handles 连播.
      if (!modal || !player.isConnected || !work || work.media_type === 'image') return
      const visible = streamerFilteredWorks()
      const position = visible.findIndex(item => String(item.aweme_id || '') === String(modal.awemeId || ''))
      if (modal.autoPlay && (position >= 0 && (position < visible.length - 1 || state.streamer.hasMore))) {
        moveStreamerWork(1).catch(() => {})
      }
    })
    player.addEventListener('volumechange', () => {
      const active = state.streamer.modal
      if (!active || !player.isConnected) return
      const mutedChanged = active.muted !== !!player.muted
      active.volume = streamerVolumeValue(player.volume)
      active.muted = !!player.muted
      state.streamer.volume = active.volume
      if (mutedChanged) syncStreamerPlayerControls()
      else syncStreamerVolumeControl()
    })
    // The first paint of a work can happen before its media is known (an image
    // post whose local record has no soundtrack only gets its audio from the
    // detail request).  That placeholder element is discarded on the rebuild,
    // and the browser then fires `pause` on it — so ignore events from any
    // player that is no longer the one on screen, otherwise the stale element
    // resets `playing` right after the live one started.
    const isLivePlayer = () => player.isConnected
      && (byId('streamer-media-video') === player || byId('streamer-media-audio') === player)
    player.addEventListener('play', () => { if (state.streamer.modal && isLivePlayer()) { state.streamer.modal.playing = true; syncStreamerPlayerControls() } })
    player.addEventListener('pause', () => { if (state.streamer.modal && isLivePlayer()) { state.streamer.modal.playing = false; syncStreamerPlayerControls() } })
    if (AppPreferences.mediaAutoplay() && (player.getAttribute('src') || player.querySelector('source')) && !(isImageGallery && current.galleryPaused)) {
      const playResult = player.play()
      if (playResult && typeof playResult.catch === 'function') playResult.catch(() => {})
    }
  }
}
// The document itself is the scroll container (`.shell` / `.main` are
// `min-height: 100vh`, nothing inside owns the scrollbar), so entering the
// creator homepage from a long monitor list just needs the window at 0.
function resetPageScroll() {
  window.scrollTo(0, 0)
}
async function openStreamerHome(secUid, returnPage = 'following') {
    if (state.userAccount && state.userAccount.is_self && String(secUid) === String(state.userAccount.sec_uid)) {
      await openAccountHome(accountHomeSession && accountHomeSession.secUid === String(secUid) ? accountHomeSession.contentMode : 'post')
      state.streamer.returnPage = returnPage
      renderPage()
      return
    }
    const owner = String(secUid || '').trim()
    if (!owner) return
    clearStreamerPreloads()
    // Remember where the monitor list was, so 返回关注主播 can put the user back
    // instead of dropping them at the top of a list they had scrolled through.
    const returnScroll = window.scrollY || 0
    resetPageScroll()
    state.page = 'streamer'
  state.streamer = { ...state.streamer, catalogue: null, contentMode: 'post', supportedModes: ['post'], secUid: owner, creator: {}, works: [], source: 'local', hasMore: false, loading: true, loadingMore: false, cursor: 0, offset: 0, mode: 'local', profileKnown: false, profileLoading: true, remoteCursors: new Set(), paginationStopped: false, error: '', profileError: '', returnPage, returnScroll, modal: null, tab: 'works', subtab: 'works', savedWorksList: null, search: '', date: '' }
  const session = state.streamer
  render()
  const result = await call('creator_home', owner, false, 0, 0)
  if (state.page !== 'streamer' || state.streamer !== session) return
  // A cached online profile shows in full straight away (and offline).
  if (result && result.creator && result.creator.source === 'cached') state.streamer.profileKnown = true
  if (result) Object.assign(state.streamer, { creator: result.creator || {}, works: Array.isArray(result.works) ? result.works : [], source: result.source || 'local', hasMore: !!result.has_more, cursor: Number(result.next_cursor || 0), offset: Number(result.next_offset || 0), mode: result.next_mode === 'remote' ? 'remote' : 'local', error: result.error || '' })
  else state.streamer.error = '本地主播资料读取失败'
  state.streamer.loading = false
  render()
  const profile = await call('creator_profile', owner)
  if (state.page !== 'streamer' || state.streamer !== session) return
  // Skeletons stand in for the remote profile only until this answer arrives.
  state.streamer.profileLoading = false
  if (!profile || !profile.creator) render()
  if (profile && profile.creator) {
    if (profile.creator.source === 'remote' || profile.creator.source === 'cached') {
      state.streamer.creator = { ...state.streamer.creator, ...profile.creator }
      state.streamer.profileKnown = true
    }
    state.streamer.profileError = profile.error || ''
    render()
  }
}
function appendStreamerWorks(incoming) {
  const seen = new Set(state.streamer.works.map(work => String(work && work.aweme_id || '')))
  ;(Array.isArray(incoming) ? incoming : []).forEach(work => {
    const key = String(work && work.aweme_id || '')
    if (!key || seen.has(key)) return
    seen.add(key)
    state.streamer.works.push(work)
  })
}
// The homepage API no longer lists hidden works, so "已隐藏" pages the local
// archive instead of filtering whatever pages happen to be loaded already.
const streamerListKeys = ['works', 'source', 'hasMore', 'cursor', 'offset', 'mode', 'remoteCursors', 'paginationStopped', 'error']
async function switchStreamerSubtab(subtab) {
  const streamer = state.streamer
  if (streamer.catalogue || streamer.subtab === subtab || streamer.loading || streamer.loadingMore) {
    if (streamer.catalogue) { streamer.subtab = subtab; renderPage() }
    return
  }
  streamer.subtab = subtab
  if (subtab === 'hidden') {
    streamer.savedWorksList = Object.fromEntries(streamerListKeys.map(key => [key, streamer[key]]))
    await loadHiddenStreamerWorks()
    return
  }
  if (streamer.savedWorksList) Object.assign(streamer, streamer.savedWorksList, { loadingMore: false })
  streamer.savedWorksList = null
  renderPage()
}
async function loadHiddenStreamerWorks() {
  const session = state.streamer
  Object.assign(session, { works: [], hasMore: false, cursor: 0, offset: 0, mode: 'local', remoteCursors: new Set(), paginationStopped: false, error: '', loading: true, loadingMore: false })
  renderPage()
  const result = await call('creator_home', session.secUid, false, 0, 0, true)
  if (state.page !== 'streamer' || state.streamer !== session || session.subtab !== 'hidden') return
  if (result && !result.error) Object.assign(session, { works: Array.isArray(result.works) ? result.works : [], source: 'local', hasMore: !!result.has_more, offset: Number(result.next_offset || 0) })
  else session.error = result && result.error || '已隐藏作品读取失败'
  session.loading = false
  renderPage()
}
async function loadMoreStreamerWorks() {
  const streamer = state.streamer
  if (streamer.catalogue) return // Account previews stream subsequent pages.
  if (state.page !== 'streamer' || !streamer.secUid || streamer.loading || streamer.loadingMore || !streamer.hasMore) return
  const owner = streamer.secUid
  const mode = streamer.mode === 'remote' ? 'remote' : 'local'
  const cursor = Number(streamer.cursor || 0)
  const remoteCursors = streamer.remoteCursors || (streamer.remoteCursors = new Set())
  if (mode === 'remote' && remoteCursors.has(cursor)) {
    streamer.hasMore = false
    streamer.paginationStopped = true
    streamer.error = streamerCursorLoopError
    renderPage()
    return
  }
  streamer.loadingMore = true
  streamer.error = ''
  renderPage()
  const result = await call('creator_home', owner, mode === 'remote', mode === 'remote' ? cursor : 0, streamer.offset, streamer.subtab === 'hidden')
  if (state.page !== 'streamer' || state.streamer !== streamer) return
  if (result && !result.error) {
    if (mode === 'remote') remoteCursors.add(cursor)
    appendStreamerWorks(result.works)
    const nextMode = result.next_mode === 'remote' ? 'remote' : 'local'
    const nextCursor = Number(result.next_cursor || 0)
    const cursorLoop = !!result.has_more && nextMode === 'remote' && remoteCursors.has(nextCursor)
    Object.assign(state.streamer, {
      hasMore: !cursorLoop && !!result.has_more,
      cursor: nextCursor,
      offset: Number(result.next_offset || state.streamer.offset),
      mode: nextMode,
      paginationStopped: cursorLoop,
      error: cursorLoop ? streamerCursorLoopError : result.error || state.streamer.error,
    })
  } else {
    if (mode === 'remote') remoteCursors.delete(cursor)
    state.streamer.error = result && result.error || '更多作品加载失败，请重试'
  }
  state.streamer.loadingMore = false
  renderPage()
}
async function ensureStreamerWorkDetail(workIndex, forceRemote = false) {
  const session = state.streamer
  const work = session.works[workIndex]
  if (!work) return null
  const hasMusic = (Array.isArray(work.music_sources) && work.music_sources.some(Boolean)) || !!work.music_source
  const needsMusic = work.media_type === 'image' && !hasMusic
  if (!forceRemote && (work.media_sources || []).some(Boolean) && work.media_state !== 'partial' && !work.media_error && !work.music_error && !needsMusic) return work
  let request = !forceRemote ? streamerDetailRequests.get(work) : null
  if (!request) {
    request = session.catalogue
      ? call('account_work_detail', session.catalogue.requestId, String(work.aweme_id || ''), forceRemote)
      : call('creator_work_detail', session.secUid, String(work.aweme_id || ''), forceRemote)
    if (!forceRemote) streamerDetailRequests.set(work, request)
  }
  let detail
  try { detail = await request }
  finally { if (streamerDetailRequests.get(work) === request) streamerDetailRequests.delete(work) }
  if (state.page !== 'streamer' || state.streamer !== session || session.works[workIndex] !== work) return null
  if (!detail) {
    work.media_error = '媒体加载失败，请重试'
    return work
  }
  if (session.catalogue) detail = { ...detail, source_index: work.source_index, download_status: work.download_status, last_error: work.last_error, author_profile: work.author_profile, catalogue: true }
  if (detail.error) {
    const mediaSources = (detail.media_sources || []).some(Boolean) ? detail.media_sources : work.media_sources
    Object.assign(work, detail, { media_sources: mediaSources, media_error: detail.error, media_detail_loaded: true })
    return work
  }
  if (!(detail.media_sources || []).some(Boolean)) {
    work.media_error = '媒体加载失败，请重试'
    return work
  }
  Object.assign(work, detail, { media_error: '', media_detail_loaded: true })
  return work
}
async function openStreamerWork(workIndex, forceRemote = false) {
  const session = state.streamer
  const work = session.works[workIndex]
  if (!work) return
  streamerPreloadGeneration += 1
  const modal = { secUid: session.secUid, awemeId: String(work.aweme_id), workIndex,
    mediaIndex: 0, playing: false, mediaStarted: AppPreferences.mediaAutoplay(), muted: false, volume: streamerVolumeValue(session.volume), progress: 0, pictureProgress: 0, pictureElapsed: 0, galleryPaused: !AppPreferences.galleryAutoplay(),
    autoPlay: !!session.autoPlay, clearScreen: !!session.clearScreen, relatedOpen: false,
    loading: true, error: '', fallbackAttempted: forceRemote }
  session.modal = modal
  renderStreamerOverlay()
  const detail = await ensureStreamerWorkDetail(workIndex, forceRemote)
  if (state.page !== 'streamer' || state.streamer !== session || session.modal !== modal) return
  modal.loading = false
  modal.error = detail && detail.media_error || (!detail ? '媒体加载失败，请重试' : '')
  renderStreamerOverlay()
  if (!modal.error && work.media_type !== 'image') preloadStreamerNeighbours(session, modal).catch(() => {})
}
async function moveStreamerWork(delta) {
  const session = state.streamer
  const modal = session.modal
  if (!modal) return
  let { list, position } = streamerWorkPosition()
  if (position < 0) return
  if (delta > 0 && position === list.length - 1 && session.hasMore) {
    await loadMoreStreamerWorks()
    if (state.streamer !== session || session.modal !== modal) return
    ;({ list, position } = streamerWorkPosition())
  }
  const work = list[position + delta]
  if (!work) return
  streamerSlidePending = delta > 0 ? 1 : -1
  await openStreamerWork(session.works.indexOf(work))
}
function renderComments() {
  const comments = state.comments.length ? state.comments.map(comment => `<article class="comment"><strong>${esc(comment.nickname || '匿名')}</strong><span class="comment-meta">${esc(comment.time || '')} ${comment.ip_label ? `· ${esc(comment.ip_label)}` : ''} · ♥${Number(comment.digg_count || 0)}</span><p>${esc(comment.text || '')}</p></article>`).join('') : emptyState('暂无评论')
  const images = state.images.length ? state.images.map(image => {
    const isSelected = selected('images', image.key)
    const description = image.nickname ? `${image.nickname} 发布的图片` : '查看图片'
    return `<div class="image-cell${image.placeholder || !image.src ? ' is-placeholder' : ''}${isSelected ? ' is-selected' : ''}"><button class="image-open" type="button" title="${esc(description)}" aria-label="${esc(description)}" data-action="image-zoom" data-key="${esc(image.key)}" data-cid="${esc(image.comment_id)}" data-index="${image.index}">${image.src ? `<img src="${esc(image.src)}" alt="">` : `<span class="image-placeholder">缩略图失败</span>`}</button><label>${check('images', image.key)}<span>${esc(image.nickname || '')}</span></label></div>`
  }).join('') : emptyState('暂无图片')
  const busy = state.busy.comments
  return `<div class="stack">${bodyPanel(`<div class="form-row">${input('commentSource', state.forms.commentSource, '作品链接、分享文案或作品 ID', 'grow', busy)}${button(busy ? '处理中…' : '读取评论', 'comment-load', 'primary', busy ? 'disabled' : '', busy)}</div>`)}<div class="two-pane"><div class="panel"><div class="panel-head"><h2>评论 <span class="label">${state.comments.length}</span></h2></div><div class="comment-list">${comments}</div></div><div class="panel"><div class="panel-head"><h2>图片 <span class="label">${state.images.length}</span></h2><div class="toolbar">${state.images.length && state.selected.images.size === state.images.length ? button('取消全选', 'image-clear', 'compact', busy ? 'disabled' : '') : button('全选', 'image-select-all', 'compact', busy || !state.images.length ? 'disabled' : '')}</div></div><div class="image-grid">${images}</div>${actionBar(`<span class="label">已选 ${state.selected.images.size}</span><span class="spacer"></span>${button('下载所选', 'comment-download', 'primary', !state.selected.images.size || busy ? 'disabled' : '')}`)}</div></div></div>`
}
function savedQuality() { return (state.settings && state.settings.quality) || '最高' }
function renderHistory() {
  const result = state.history || { items: [], total: 0, page: 1, size: 50 }
  const rows = result.items.length ? `<div class="table-wrap"><table><thead><tr><th style="width:150px">作品 ID</th><th>标题</th><th style="width:140px">作者</th><th style="width:80px">类型</th><th style="width:120px">发布时间</th><th>本地路径</th></tr></thead><tbody>${result.items.map(row => `<tr><td title="${esc(row.aweme_id)}">${esc(row.aweme_id)}</td><td class="title" title="${esc(row.title)}"><button type="button" class="work-detail-trigger" data-action="work-details-open" data-scope="history" data-id="${esc(row.aweme_id)}">${esc(row.title || '无标题')}</button></td><td title="${esc(row.author_name || '—')}">${esc(row.author_name || '—')}</td><td>${esc(typeLabels[row.aweme_type] || row.aweme_type || '—')}</td><td>${esc(row.create_time || '—')}</td><td title="${esc(row.file_path)}">${esc(row.file_path || '—')}</td></tr>`).join('')}</tbody></table></div>` : state.errors.history ? `<div class="empty"><strong>读取失败</strong><span>${esc(state.errors.history)}</span>${button('重试', 'history-refresh', 'secondary')}</div>` : emptyState('暂无下载记录', '下载完成的作品会记录在这里，可以按作者、标题和类型查找')
  const page = Math.max(1, Number(result.page || 1)); const max = Math.max(1, Math.ceil(Number(result.total || 0) / Number(result.size || 50)))
  const error = state.errors.history && result.items.length ? `<div class="notice error" role="alert">刷新失败，显示的是上次结果：${esc(state.errors.history)} ${button('重试', 'history-refresh', 'secondary', '', false)}</div>` : ''
  return `<div class="stack">${bodyPanel(`<div class="form-row">${input('historyAuthor', state.forms.historyAuthor, '作者')}${input('historyTitle', state.forms.historyTitle, '标题', 'grow')}<select data-bind="historyType" aria-label="作品类型"><option value=""${state.forms.historyType === '' ? ' selected' : ''}>全部类型</option><option value="video"${state.forms.historyType === 'video' ? ' selected' : ''}>视频</option><option value="gallery"${state.forms.historyType === 'gallery' ? ' selected' : ''}>图文</option><option value="music"${state.forms.historyType === 'music' ? ' selected' : ''}>音乐</option></select>${button('查询', 'history-refresh', 'primary')}</div>`)}<div class="panel">${error}${rows}${actionBar(`<span class="label">共 ${Number(result.total || 0)} 条 · 第 ${page} / ${max} 页</span><span class="spacer"></span>${button('上一页', 'history-prev', 'secondary', page <= 1 ? 'disabled' : '')}${button('下一页', 'history-next', 'secondary', page >= max ? 'disabled' : '')}`)}</div></div>`
}
// Segmented thumbs slide from where they were before a re-render.
function segmentThumbPositions(root) {
  const positions = {}
  root.querySelectorAll('.segmented[aria-label]').forEach(group => {
    const thumb = group.querySelector('.segment-thumb')
    if (thumb && group.classList.contains('has-thumb')) positions[group.getAttribute('aria-label')] = { x: thumb.style.transform, w: thumb.style.width }
  })
  return positions
}
function placeSegmentThumb(group, animate) {
  const thumb = group.querySelector('.segment-thumb')
  const current = group.querySelector('.segment.checked')
  if (!thumb || !current) { group.classList.remove('has-thumb'); return }
  thumb.classList.toggle('is-moving', !!animate)
  thumb.style.width = current.offsetWidth + 'px'
  thumb.style.transform = `translateX(${current.offsetLeft}px)`
  group.classList.add('has-thumb')
}
function syncSegmentThumbs(root, previous = {}) {
  root.querySelectorAll('.segmented').forEach(group => {
    const before = previous[group.getAttribute('aria-label')]
    const thumb = group.querySelector('.segment-thumb')
    if (before && thumb) {
      thumb.classList.remove('is-moving')
      thumb.style.width = before.w
      thumb.style.transform = before.x
      group.classList.add('has-thumb')
      void thumb.offsetWidth
      placeSegmentThumb(group, true)
    } else {
      placeSegmentThumb(group, false)
    }
  })
}
function pageHtml() {
  if (state.startup === 'settings') return '<section class="panel startup-panel" role="status"><span class="loading-spinner" aria-hidden="true"></span><strong>正在读取本地设置…</strong><span>界面已启动，数据加载完成后即可使用。</span></section>'
  if (state.startup === 'error') return `<section class="panel startup-panel" role="alert"><strong>本地设置读取失败</strong><span>请重试；详细信息可在日志中查看。</span>${button('重试', 'startup-retry', 'primary')}</section>`
  return ({ link: renderLink, user: renderUser, following: renderFollowing, streamer: renderStreamerHome, comments: renderComments, history: renderHistory, settings: renderSettings }[state.page] || renderLink)()
}
const focusDatasetKeys = ['bind', 'action', 'id', 'scope', 'column', 'key', 'cid', 'index', 'awemeId', 'tab', 'category', 'uiFocusKey']
function describeFocus(node) {
  if (!(node instanceof HTMLElement)) return null
  const descriptor = { tag: node.tagName, inDialog: !!node.closest('[aria-modal="true"], #streamer-modal') }
  if (node instanceof HTMLInputElement && node.type === 'radio') descriptor.radioValue = node.value
  focusDatasetKeys.forEach(key => {
    if (node.dataset[key] != null) descriptor[key] = node.dataset[key]
  })
  if (!descriptor.bind && !descriptor.action) return null
  if (typeof node.selectionStart === 'number') {
    descriptor.selectionStart = node.selectionStart
    descriptor.selectionEnd = node.selectionEnd
    descriptor.selectionDirection = node.selectionDirection
  }
  return descriptor
}
function focusMatch(root, descriptor) {
  if (!root || !descriptor) return null
  return [...root.querySelectorAll('[data-bind], [data-action]')].find(node =>
    node.tagName === descriptor.tag
      && (descriptor.radioValue == null || node.value === descriptor.radioValue)
      && focusDatasetKeys.every(key => descriptor[key] == null || String(node.dataset[key] || '') === descriptor[key])
  ) || null
}
function restoreFocus(root, descriptor) {
  const node = focusMatch(root, descriptor)
  if (!node || node.disabled) return false
  node.focus({ preventScroll: true })
  if (descriptor.selectionStart != null && typeof node.setSelectionRange === 'function') {
    try { node.setSelectionRange(descriptor.selectionStart, descriptor.selectionEnd, descriptor.selectionDirection || 'none') } catch {}
  }
  return true
}
function focusFallback(root) {
  const target = root && root.querySelector('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href]')
  if (target) target.focus({ preventScroll: true })
  return !!target
}
function activeModal() {
  if (window.AppUI) return AppUI.activeModal()
  return document.querySelector('.image-overlay[role="dialog"]') || document.querySelector('.following-overlay[role="dialog"], .monitor-delete-overlay [role="dialog"], #streamer-modal')
}
function focusModal(modal) {
  const target = modal && modal.querySelector('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]')
  if (target) target.focus({ preventScroll: true })
  else if (modal && modal.focus) {
    modal.setAttribute('tabindex', '-1')
    modal.focus({ preventScroll: true })
  }
}
const riskTypeLabels = {
  http_429: '请求过快（HTTP 429）', verify_ticket: '触发验证票据', captcha: '触发验证码',
  empty_200: '空响应（HTTP 200）', non_json_200: '非 JSON 响应（HTTP 200）',
  http_403: '请求被拒绝（HTTP 403）', signature_rejected: '签名被拒绝',
  login_required: '需要登录',
}
const definiteRiskTypeNames = new Set(['http_429', 'verify_ticket', 'captcha'])
const suspectedRiskTypeNames = new Set(['empty_200', 'non_json_200', 'http_403', 'signature_rejected'])
function riskCategory(type, category) {
  if (category === 'risk' || category === 'diagnostic') return category
  return type === 'login_required' || String(type).startsWith('api_status_') ? 'diagnostic' : 'risk'
}
function riskTypeLabel(type) {
  const name = String(type || '未知事件')
  if (riskTypeLabels[name]) return riskTypeLabels[name]
  const status = name.match(/^api_status_(\d+)$/)
  return status ? `接口业务状态码 ${status[1]}` : name.replaceAll('_', ' ')
}
function formatRiskTime(value) {
  if (value === null || value === undefined || value === '') return '—'
  const numeric = Number(value)
  const date = Number.isFinite(numeric) && String(value).trim() !== ''
    ? new Date(numeric < 1e12 ? numeric * 1000 : numeric)
    : new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN')
}
function riskTypeGroup(title, tone, badge, types) {
  const rows = types.map(item => {
    const type = String(item.type || '')
    return `<li class="risk-type-row"><div class="risk-type-name"><strong><span class="risk-category ${tone}">${badge}</span>${esc(riskTypeLabel(type))}</strong><small>最近：${esc(formatRiskTime(item.last_seen))}</small></div><div class="risk-count"><small>24h</small><strong>${Number(item.count_24h) || 0}</strong></div><div class="risk-count"><small>累计</small><strong>${Number(item.count_total) || 0}</strong></div></li>`
  }).join('')
  return `<section class="risk-section"><h3>${title}</h3>${rows ? `<ul class="risk-type-list">${rows}</ul>` : '<div class="risk-empty">暂无记录</div>'}</section>`
}
function renderRiskReport() {
  const root = byId('risk-report')
  if (!root) return
  const current = state.riskReport
  if (current.status === 'loading' || current.status === 'idle') {
    root.innerHTML = '<div class="risk-report-state" role="status">正在读取风控统计…</div>'
    return
  }
  if (current.status === 'error') {
    root.innerHTML = `<div class="risk-report-state error" role="alert">${esc(current.error || '读取风控统计失败')}<div><button type="button" class="button secondary risk-refresh" data-action="risk-refresh">重试</button></div></div>`
    return
  }
  const report = current.report || {}
  const reportError = String(report.error || '').trim()
  const totalFields = ['risk_24h', 'risk_total', 'diagnostics_24h', 'diagnostics_total']
  const hasValidTotals = totalFields.every(field => report[field] !== null && report[field] !== undefined && String(report[field]).trim() !== '' && Number.isFinite(Number(report[field])))
  if (reportError && !hasValidTotals) {
    root.innerHTML = `<div class="risk-report-state error" role="alert">${esc(reportError)}<div><button type="button" class="button secondary risk-refresh" data-action="risk-refresh">重试</button></div></div>`
    return
  }
  const types = Array.isArray(report.by_type) ? report.by_type : []
  const events = Array.isArray(report.recent_events) ? report.recent_events.slice(0, 100) : []
  const definiteTypes = types.filter(item => definiteRiskTypeNames.has(String(item.type)))
  const suspectedTypes = types.filter(item => suspectedRiskTypeNames.has(String(item.type)))
  const otherRiskTypes = types.filter(item => riskCategory(item.type, item.category) === 'risk' && !definiteRiskTypeNames.has(String(item.type)) && !suspectedRiskTypeNames.has(String(item.type)))
  const diagnosticTypes = types.filter(item => riskCategory(item.type, item.category) === 'diagnostic')
  const eventRows = events.map(item => {
    const category = riskCategory(item.type, item.category)
    const status = [item.http_status == null ? '' : `HTTP ${item.http_status}`, item.api_status == null ? '' : `API ${item.api_status}`].filter(Boolean).join(' · ')
    const path = String(item.path || '—').split(/[?#]/, 1)[0]
    const reason = String(item.reason || '无补充说明')
    return `<li class="risk-event"><div class="risk-event-head"><time>${esc(formatRiskTime(item.timestamp))}</time><span class="risk-category ${category}">${category === 'risk' ? '风控' : '诊断'}</span><strong>${esc(riskTypeLabel(item.type))}</strong>${status ? `<span class="risk-event-status">${esc(status)}</span>` : ''}</div><code class="risk-event-path">${esc(path)}</code><div class="risk-event-reason">${esc(reason.length > 600 ? reason.slice(0, 600) + '…' : reason)}</div></li>`
  }).join('')
  root.innerHTML = `${reportError ? `<div class="risk-report-warning" role="status"><span>部分统计读取失败：${esc(reportError)}</span><button type="button" class="button secondary risk-refresh" data-action="risk-refresh">重试</button></div>` : ''}<div class="risk-report-heading"><h3>风控概览</h3></div><div class="risk-metrics"><article class="risk-metric risk"><div class="risk-metric-title">风控信号（含疑似）</div><div class="risk-metric-values"><strong>${Number(report.risk_24h) || 0}</strong><span>近 24 小时</span></div><div class="risk-metric-values"><strong>${Number(report.risk_total) || 0}</strong><span>累计</span></div></article><article class="risk-metric diagnostic"><div class="risk-metric-title">其他接口诊断</div><div class="risk-metric-values"><strong>${Number(report.diagnostics_24h) || 0}</strong><span>近 24 小时</span></div><div class="risk-metric-values"><strong>${Number(report.diagnostics_total) || 0}</strong><span>累计</span></div></article></div>${riskTypeGroup('明确风控', 'definite', '明确', definiteTypes)}${riskTypeGroup('疑似拦截', 'suspected', '疑似', suspectedTypes)}${otherRiskTypes.length ? riskTypeGroup('其他风控信号', 'risk', '风控', otherRiskTypes) : ''}${riskTypeGroup('其他接口诊断', 'diagnostic', '诊断', diagnosticTypes)}<section class="risk-section"><div class="risk-section-heading"><h3>最近事件</h3><span>${events.length} 条<button type="button" class="button secondary risk-refresh" data-action="risk-refresh">刷新</button></span></div>${eventRows ? `<ol class="risk-event-list">${eventRows}</ol>` : '<div class="risk-empty">暂无风控或诊断事件</div>'}</section>`
}
function syncLogDrawer() {
  const logsActive = state.logTab !== 'risk'
  byId('log-tab-logs').setAttribute('aria-selected', String(logsActive))
  byId('log-tab-logs').tabIndex = logsActive ? 0 : -1
  byId('log-tab-risk').setAttribute('aria-selected', String(!logsActive))
  byId('log-tab-risk').tabIndex = logsActive ? -1 : 0
  byId('log-panel-logs').hidden = !logsActive
  byId('log-panel-risk').hidden = logsActive
  byId('log-text').textContent = state.logs.join('')
  renderRiskReport()
}
function setLogTab(tab) {
  if (tab !== 'logs' && tab !== 'risk') return
  state.logTab = tab
  syncLogDrawer()
  if (tab === 'risk') loadRiskReport()
}
async function loadRiskReport() {
  const request = ++riskReportRequest
  state.riskReport = { status: 'loading', report: null, error: '' }
  renderRiskReport()
  try {
    const api = bridge()
    if (!api || typeof api.get_risk_report !== 'function') throw new Error(api ? '缺少风控统计接口' : '应用桥接尚未就绪')
    const report = await api.get_risk_report()
    if (request !== riskReportRequest) return
    if (!report || typeof report !== 'object') throw new Error('风控统计接口未返回有效数据')
    state.riskReport = { status: 'ready', report, error: '' }
  } catch (error) {
    if (request !== riskReportRequest) return
    state.riskReport = { status: 'error', report: null, error: error instanceof Error ? error.message : String(error) }
  }
  renderRiskReport()
}
function queueRiskReportRefresh() {
  if (riskRefreshTimer) return
  riskRefreshTimer = setTimeout(() => {
    riskRefreshTimer = null
    if (state.logOpen && state.logTab === 'risk') loadRiskReport()
  }, 120)
}
// The table's horizontal overflow container prevents CSS sticky from following
// the page scroll, so use a synchronized header outside that container.
let monitorFixedHeader = null
let monitorHeaderFrame = 0
let monitorObservedWrap = null
const monitorHeaderObserver = new ResizeObserver(syncMonitorFixedHeader)
function resetMonitorFixedHeader() {
  if (monitorFixedHeader) monitorFixedHeader.remove()
  monitorFixedHeader = null
}
function syncMonitorFixedHeader() {
  const table = state.page === 'following'
    ? document.querySelector('#content .monitor-panel .table-wrap table') : null
  if (!table || !table.tHead) {
    resetMonitorFixedHeader()
    monitorHeaderObserver.disconnect()
    monitorObservedWrap = null
    return
  }
  if (monitorObservedWrap !== table.parentElement) {
    monitorHeaderObserver.disconnect()
    monitorObservedWrap = table.parentElement
    monitorHeaderObserver.observe(monitorObservedWrap)
    // At wide viewports the content is capped: collapsing the sidebar moves
    // the table without resizing it. Main still resizes throughout animation.
    const main = table.closest('.main')
    if (main) monitorHeaderObserver.observe(main)
  }
  const head = table.tHead
  const headRect = head.getBoundingClientRect()
  const tableRect = table.getBoundingClientRect()
  const viewportTop = titlebarHeight()
  if (headRect.top >= viewportTop || tableRect.bottom <= viewportTop) {
    resetMonitorFixedHeader()
    return
  }
  if (!monitorFixedHeader) {
    monitorFixedHeader = document.createElement('div')
    monitorFixedHeader.className = 'monitor-panel monitor-fixed-header'
    const copy = document.createElement('table')
    copy.appendChild(head.cloneNode(true))
    copy.querySelectorAll('[data-action]').forEach(node => { node.dataset.uiFocusKey = `fixed-${node.dataset.action}` })
    monitorFixedHeader.appendChild(copy)
    document.body.appendChild(monitorFixedHeader)
  }
  const wrap = table.parentElement
  const panelRect = wrap.closest('.panel').getBoundingClientRect()
  const copy = monitorFixedHeader.firstElementChild
  // Match the complete panel (including its side borders) with fractional
  // geometry, rather than rounding the inner viewport with clientWidth.
  monitorFixedHeader.style.left = `${panelRect.left}px`
  monitorFixedHeader.style.width = `${panelRect.width}px`
  copy.style.width = `${tableRect.width}px`
  copy.style.transform = `translateX(${-wrap.scrollLeft}px)`
  Array.from(head.rows[0].cells).forEach((cell, index) => {
    const target = copy.tHead.rows[0].cells[index]
    const rect = cell.getBoundingClientRect()
    target.style.width = `${rect.width}px`
    target.style.display = getComputedStyle(cell).display
  })
  const fixedHeight = monitorFixedHeader.getBoundingClientRect().height
  monitorFixedHeader.style.top = `${Math.min(viewportTop, tableRect.bottom - fixedHeight)}px`
}
function scheduleMonitorFixedHeader() {
  if (monitorHeaderFrame) return
  monitorHeaderFrame = requestAnimationFrame(() => {
    monitorHeaderFrame = 0
    syncMonitorFixedHeader()
  })
}
document.addEventListener('scroll', scheduleMonitorFixedHeader, { capture: true, passive: true })
window.addEventListener('resize', scheduleMonitorFixedHeader)
let renderedPage = ''
function renderPage() {
  if (AppRenderBatch.defer('page', renderPage)) return
  const wasModal = activeModal()
  const previousFocusNode = document.activeElement
  const previousFocus = describeFocus(previousFocusNode)
  if (state.page !== 'following') state.monitorMenuId = ''
  if (state.page !== 'user') state.subscriptionMenuId = ''
  byId('page-title').textContent = meta[state.page]
  document.querySelectorAll('.nav button[data-page]').forEach(node => {
    const active = node.dataset.page === state.page
    node.classList.toggle('active', active)
    if (active) node.setAttribute('aria-current', 'page')
    else node.removeAttribute('aria-current')
  })
  const content = byId('content')
  const overlayHost = byId('ui-overlay-host')
  const pageScroll = AppUI.scrollState(content)
  const overlayScroll = AppUI.scrollState(overlayHost)
  const thumbs = segmentThumbPositions(content)
  const pageChanged = renderedPage !== state.page
  renderedPage = state.page
  resetMonitorFixedHeader()
  content.innerHTML = pageHtml()
  AppUI.mountPageOverlays(content)
  WorkDetails.syncPage(state.page)
  if (!pageChanged) {
    AppUI.restoreScroll(content, pageScroll)
    AppUI.restoreScroll(overlayHost, overlayScroll)
  }
  syncSegmentThumbs(content, pageChanged ? {} : thumbs)
  if (pageChanged) {
    content.classList.remove('page-enter')
    void content.offsetWidth
    content.classList.add('page-enter')
  }
  const view = creatorView()
  if (view && view.applyPageChrome) view.applyPageChrome()
  renderStreamerOverlay()
  AppUI.syncModal()
  const deleteDialog = document.querySelector('.monitor-delete-dialog')
  if (deleteDialog) deleteDialog.setAttribute('tabindex', '-1')
  alignMonitorSwitches()
  syncMonitorFixedHeader()
  alignMonitorRowMenu()
  alignSubscriptionMenu()
  byId('log-drawer').hidden = !state.logOpen
  syncLogDrawer()
  const modal = activeModal()
  if (modal) {
    if (!wasModal) {
      if (!state.dialogFocusReturn) state.dialogFocusReturn = previousFocus
      focusModal(modal)
    } else if (!restoreFocus(modal, previousFocus && previousFocus.inDialog ? previousFocus : null)) {
      focusModal(modal)
    }
  } else if (wasModal) {
    const returnFocus = state.dialogFocusReturn
    state.dialogFocusReturn = null
    if (!restoreFocus(document, returnFocus)) focusFallback(byId('content'))
  } else if (previousFocus && !previousFocusNode.isConnected && !restoreFocus(document, previousFocus)) {
    focusFallback(byId('content'))
  }
}
function updateStatusDot(id, text) {
  const dot = byId(id)
  const label = String(text || '').trim()
  const progress = label.match(/(?:^|\s)(\d+)\/(\d+)(?:\s|$)/)
  dot.className = 'status-dot'
  if (/(失败|中断)/.test(label)) dot.classList.add('bad')
  else if (progress && Number(progress[1]) >= Number(progress[2])) dot.classList.add('ok')
  else if (/^(正在扫描|扫描|全量扫描|下载)/.test(label)) dot.classList.add('active')
}
function applyFullScanProgress(snapshot) {
  const wasActive = FullScanProgress.active(state.fullScanTask)
  const accepted = FullScanProgress.accept(state.fullScanTask, snapshot)
  if (accepted === state.fullScanTask) return
  state.fullScanTask = accepted
  if (wasActive && !FullScanProgress.active(accepted) && state.page === 'following') {
    if (state.downloadStatus === FullScanProgress.format(accepted).download) state.downloadStatus = ''
  }
  renderStatus()
}
async function requestFullScan(ids) {
  if (state.fullScanStarting) return false
  const targets = [...new Set(ids.map(String).filter(Boolean))]
  if (!FullScanProgress.active(state.fullScanTask)) {
    state.fullScanStarting = { task_id: 0, status: 'scanning', scan_total: targets.length,
      scan_done: 0, scan_failed: 0, download_total: 0, download_done: 0,
      download_success: 0, download_failed: 0 }
    renderStatus()
  }
  try {
    const accepted = await call('full_scan_monitors', targets)
    applyFullScanProgress(await call('full_scan_progress'))
    return accepted
  } finally { state.fullScanStarting = null; renderStatus() }
}
function currentUserDownloadTask() {
  const { starting, download } = state.userTaskProgress
  if (starting) return starting
  if (AccountTaskProgress.active(download)) return download
  const owner = String(state.userAccount && state.userAccount.sec_uid || '')
  const mode = String(state.page === 'streamer' ? state.streamer.contentMode : state.forms.userMode)
  return download && download.owner === owner && download.mode === mode ? download : null
}
// ── footer ──────────────────────────────────────────────────────
// Left lane: what is being read, parsed or scanned.  A job the user started
// wins; otherwise the backend's scan line (pauses, scans, comment sync); a
// finished job's last line stays for FINISHED_MS, then the lane is 空闲.
// Right lane: downloads only — the page's own download, else the queue.
const FINISHED_MS = 5000
function setActivity(key, text, options = {}) {
  if (!text) delete state.activities[key]
  else state.activities[key] = { text: String(text), detail: String(options.detail || ''),
    running: options.running !== false, at: Date.now() }
  renderStatus()
}
function latest(items) { return items.sort((a, b) => b.at - a.at)[0] }
function leftLane(userTask, userSnapshot) {
  const now = Date.now()
  const entries = Object.values(state.activities)
  const running = latest(entries.filter(item => item.running))
  if (running) return running
  // The account page's own scan, while its task is still going.
  if (userTask && userSnapshot && AccountTaskProgress.active(userSnapshot)) {
    return { text: userTask.scan, detail: userTask.detail, running: userSnapshot.scan_state === 'scanning' }
  }
  const scan = state.scanLine
  if (scan.text) {
    if (scan.countdown > 0) {
      const left = Math.max(0, scan.countdown - Math.floor((now - scan.at) / 1000))
      return { text: `${scan.text} ${formatCountdown(left)}`, detail: scan.detail || '', running: false }
    }
    return scan
  }
  return latest(entries.filter(item => now - item.at < FINISHED_MS))
    || { text: '空闲', detail: '', running: false }
}
function formatCountdown(seconds) {
  const value = Math.max(0, Math.floor(seconds))
  if (value < 60) return `${value}s`
  if (value < 3600) return `${Math.floor(value / 60)}分${String(value % 60).padStart(2, '0')}秒`
  return `${Math.floor(value / 3600)}小时${String(Math.floor(value % 3600 / 60)).padStart(2, '0')}分`
}
function pageDownload() {
  const item = state.pageDownloads[state.page]
  return item && (item.running || Date.now() - item.at < FINISHED_MS) ? item.text : ''
}
function renderStatus() {
  if (AppRenderBatch.defer('status', renderStatus)) return
  const busyScopes = Object.entries(state.busy).filter(([, value]) => value).map(([key]) => key)
  const userSurface = state.page === 'user' || (state.page === 'streamer' && state.streamer.catalogue)
  const monitorSnapshot = state.fullScanStarting || state.fullScanTask
  const currentDownload = currentUserDownloadTask()
  const userSnapshot = userSurface
    ? currentDownload || (state.page === 'streamer' ? state.userTaskProgress.browse : null) : null
  const userTask = userSnapshot ? AccountTaskProgress.format(userSnapshot) : null
  const monitorTask = !userSurface && (!!state.fullScanStarting || FullScanProgress.active(monitorSnapshot))
    ? FullScanProgress.format(monitorSnapshot) : null
  const left = leftLane(userTask, userSnapshot)
  // The queue's own count (the same as the download panel's); a full scan's
  // batch count only while the queue has not reported yet.
  const queueText = pageDownload() || state.downloadStatus
  const downloadText = userTask ? userTask.download
    : queueText || (monitorTask ? monitorTask.download : '')
  byId('status').classList.add('status-dual')
  byId('status-text').textContent = left.text
  byId('status-dots').textContent = left.running ? '.'.repeat(state.dotPhase) : ''
  byId('status-detail').textContent = left.detail || ''
  byId('status-download').textContent = downloadText || '—'
  const scanDot = byId('status-scan-dot')
  scanDot.className = 'status-dot'
  if (left.running) scanDot.classList.add('active')
  else if (/失败/.test(left.text) || /^(风控|未登录)/.test(left.text)) scanDot.classList.add('bad')
  else if (left.text !== '空闲') scanDot.classList.add('ok')
  updateStatusDot('status-download-dot', downloadText)
  if (userTask) byId('status-download-dot').className = `status-dot ${userTask.downloadTone}`
  else if (monitorTask && !queueText) byId('status-download-dot').className = `status-dot ${monitorTask.downloadTone}`
  byId('busy-indicator').hidden = !['settings', 'data'].includes(state.startup) && busyScopes.length === 0
  const labels = { link: '链接下载', user: '用户作品', comments: '评论', localValidate: '文件校验' }
  byId('busy-indicator').textContent = state.startup === 'settings' ? '正在读取设置…'
    : state.startup === 'data' ? '正在加载本地数据…' : busyScopes.length
    ? busyScopes.map(key => labels[key] || '处理中').join(' · ')
    : ''
}
// Animated dots, countdowns and expiring final lines tick once a second.
window.setInterval(() => {
  state.dotPhase = state.dotPhase % 3 + 1
  const now = Date.now()
  for (const [key, item] of Object.entries(state.activities)) {
    if (!item.running && now - item.at >= FINISHED_MS) delete state.activities[key]
  }
  renderStatus()
}, 1000)
function render() { renderPage(); renderStatus() }
// Background downloads publish a refresh per work; fetch at most one snapshot
// per interval and skip re-rendering when nothing changed.
const MONITOR_REFRESH_MIN_MS = 1000
const monitorRefresh = { running: false, again: false, last: 0, snapshot: '' }
function requestMonitorRefresh() {
  if (monitorRefresh.running) { monitorRefresh.again = true; return }
  monitorRefresh.running = true
  const wait = Math.max(0, MONITOR_REFRESH_MIN_MS - (Date.now() - monitorRefresh.last))
  window.setTimeout(async () => {
    try {
      const rows = await call('monitors')
      if (rows) applyMonitorSnapshot(rows)
    } finally {
      monitorRefresh.last = Date.now()
      monitorRefresh.running = false
      if (monitorRefresh.again) { monitorRefresh.again = false; requestMonitorRefresh() }
    }
  }, wait)
}
function applyMonitorSnapshot(rows) {
  const snapshot = JSON.stringify(rows || [])
  const changed = snapshot !== monitorRefresh.snapshot
  monitorRefresh.snapshot = snapshot
  applyMonitorRows(rows || [])
  if (changed && state.page === 'following') renderPage()
}
// Rows are rebuilt on refresh; a stationary cursor would drop :hover until the
// mouse moves, so the hovered row is tracked here and rendered with a class.
function setMonitorHover(id) {
  if (state.monitorHoverId === id) return
  state.monitorHoverId = id
  document.querySelectorAll('.monitor-panel tbody tr[data-id]').forEach(node => node.classList.toggle('is-hovered', node.dataset.id === id))
}
function applyMonitorRows(rows) {
  state.errors.monitor = ''
  const next = Array.isArray(rows) ? rows : []
  const transient = state.monitors.filter(row => row && (row._pending || row._optimistic))
  const selected = new Set(state.monitors.filter(row => row && row._selected).map(row => String(row.sec_uid)))
  const seen = {}
  next.forEach(row => {
    const key = String(row.sec_uid)
    seen[key] = true
    if (selected.has(key)) row._selected = true
    if (key in pendingMonitorComments) {
      if (Boolean(row.comments_enabled) === Boolean(pendingMonitorComments[key])) delete pendingMonitorComments[key]
      else row.comments_enabled = pendingMonitorComments[key]
    }
    if (!(key in pendingMonitorEnabled)) return
    const wanted = pendingMonitorEnabled[key]
    if (Boolean(row.enabled) === Boolean(wanted)) delete pendingMonitorEnabled[key]
    else row.enabled = wanted
  })
  Object.keys(pendingMonitorEnabled).forEach(key => { if (!seen[key]) delete pendingMonitorEnabled[key] })
  Object.keys(pendingMonitorComments).forEach(key => { if (!seen[key]) delete pendingMonitorComments[key] })
  // Keep optimistic rows until the worker publishes either a resolved creator
  // or an add failure.  Direct /user/ links can also be reconciled from the
  // persisted sec_uid if the refresh wins the event race.
  const unresolved = transient.filter(item => !next.some(row => {
    const match = String(item._pending_url || '').match(/\/user\/([A-Za-z0-9_-]+)/i)
    return item._optimistic
      ? String(row.sec_uid || '') === String(item.sec_uid || '')
      : match && String(row.sec_uid || '') === match[1]
  }))
  state.monitors = next.concat(unresolved)
}
function addPendingMonitorRows(raw) {
  const lines = String(raw || '').split(/\r?\n/).map(value => value.trim()).filter(Boolean)
  const existing = new Set(state.monitors.filter(row => row && row._pending).map(row => String(row._pending_url || '')))
  const added = []
  const stamp = Date.now()
  lines.forEach((url, index) => {
    if (existing.has(url)) return
    added.push(url)
    existing.add(url)
    state.monitors.push({
      sec_uid: `pending:${stamp}:${index}`,
      nickname: '正在解析…',
      enabled: true,
      last_check_note: '解析中',
      homepage_url: url,
      _pending: true,
      _pending_url: url,
    })
  })
  return added
}
function removePendingMonitor(url) {
  const value = String(url || '')
  state.monitors = state.monitors.filter(row => !(row && row._pending && String(row._pending_url || '') === value))
}
function applyMonitorAdded(event) {
  removePendingMonitor(event.url)
  const row = { ...(event.row || {}), _pending: false, _optimistic: true }
  const existing = state.monitors.find(item => String(item.sec_uid) === String(row.sec_uid))
  if (existing) Object.assign(existing, row)
  else state.monitors.unshift(row)
  if (state.page === 'following') renderPage()
}
function formValues() {
  const f = state.forms
  return { links: f.links.split(/\r?\n/).map(v => v.trim()).filter(Boolean), path: f.path, mode: state.settings.mode || 'post', limit: Number(f.userLimit || 0), increase: !!f.increase, retry: Number(f.retry || 3), quality: f.quality, browser_fallback: !!f.browserFallback, cover: !!f.cover, music: !!f.music, avatar: !!f.avatar, json: !!f.json, homepage_screenshot: !!f.homepageScreenshot, live_photo_merge: !!f.livePhotoMerge }
}
function applySettingsState(settings) {
  const initializing = !state.settingsSnapshot
  state.settings = settings || {}
  SettingsUI.applySaved(state.settings, state.forms)
  if (initializing) {
    const mode = state.settings.mode || 'post'
    const increase = state.settings.increase_by_mode || {}
    Object.assign(state.forms, {
      links: (state.settings.links || []).join('\n'), userMode: mode, userLimit: state.settings.limit || 0,
      userIncrease: modeSupportsIncremental(mode) && (Object.hasOwn(increase, mode) ? !!increase[mode] : true),
      increase: !!state.settings.increase,
    })
  }
  applyUiPreferences(); cacheTheme(state.theme)
  state.settingsSnapshot = settingsSnapshot()
}
async function refreshHistory() {
  const request = ++historyRequestGeneration
  const result = await call('history', { page: state.history.page || 1, size: 50, author: state.forms.historyAuthor, title: state.forms.historyTitle, type: state.forms.historyType })
  if (result && request === historyRequestGeneration) {
    state.errors.history = ''
    state.history = result
    if (state.page === 'history') renderPage()
  }
}
async function bootstrap() {
  if (bootstrapRunning) return
  bootstrapRunning = true
  state.startup = 'settings'
  render()
  try {
    const settings = await call('state')
    if (!settings) { state.startup = 'error'; render(); return }
    applySettingsState(settings)
    if (!settings.cookie_configured) state.page = 'settings'
    state.startup = 'data'
    // Paint usable controls as soon as preferences arrive. A slow history or
    // creator query must not hold the first page behind its entire snapshot.
    render()
    startPolling()
    if (settings.cookie_configured) showResumeUserDownloadPrompt()
    const historyRequest = ++historyRequestGeneration
    const initialMonitors = state.monitors, initialSubscriptions = state.subscriptions
    await Promise.all([
      call('full_scan_progress').then(applyFullScanProgress),
      call('scan_status').then(line => { if (line) applyEvent({ type: 'scan_status', ...line }) }),
      call('monitors').then(rows => {
        if (rows && state.monitors === initialMonitors) {
          applyMonitorRows(rows)
          if (state.page === 'following') renderPage()
        }
      }),
      call('history', { page: 1, size: 50 }).then(history => {
        if (history && historyRequest === historyRequestGeneration) {
          state.history = history
          if (state.page === 'history') renderPage()
        }
      }),
      call('subscriptions').then(rows => {
        if (Array.isArray(rows) && state.subscriptions === initialSubscriptions) {
          state.subscriptions = rows
          if (state.page === 'user') renderPage()
        }
      }),
    ])
  } catch (error) {
    if (state.startup === 'settings') { state.startup = 'error'; renderPage() }
    showToast(error instanceof Error ? error.message : String(error))
  } finally {
    if (state.startup === 'data') state.startup = ''
    bootstrapRunning = false
    renderStatus()
  }
}
function startPolling() {
  if (pollingStarted) return
  pollingStarted = true
  window.setInterval(async () => {
    if (pollInFlight) return
    pollInFlight = true
    try {
      const events = await call('poll')
      applyEventBatch(events || [])
      applyFullScanProgress(await call('full_scan_progress'))
    } finally {
      pollInFlight = false
    }
  }, 220); window.setInterval(tickMonitorTimes, 20000); window.setInterval(tickFullScanSchedule, 60000)
}
document.addEventListener('mouseover', event => {
  const row = event.target.closest && event.target.closest('.monitor-panel tbody tr[data-id]')
  setMonitorHover(row ? row.dataset.id : '')
})
document.documentElement.addEventListener('mouseleave', () => setMonitorHover(''))
// The add-subscription dialog keeps its own fields out of state.forms.
function bindSubscriptionField(node) {
  const field = { subscriptionUrl: 'url', subscriptionLike: 'like', subscriptionCollect: 'collect', subscriptionSource: 'source' }[node.dataset.bind]
  if (!field) return false
  state.subscriptionAdd[field] = node.type === 'checkbox' ? node.checked : node.value
  if (field === 'source') { state.subscriptionAdd.error = ''; renderPage(); return true }
  if (field === 'url') {
    const other = !!String(node.value || '').trim()
    const box = document.querySelector('[data-bind="subscriptionCollect"]')
    if (box) { box.disabled = other; box.checked = !other && !!state.subscriptionAdd.collect }
  }
  return true
}
document.addEventListener('input', event => {
  const node = event.target.closest('[data-bind]')
  if (!node) return
  if (bindSubscriptionField(node)) return
  if (node.dataset.bind === 'settingsSearch') {
    SettingsUI.view.query = node.value
    if (!event.isComposing) renderSettingsResults()
    return
  }
  if (node.dataset.bind === 'streamerSearch') {
    state.streamer.search = node.value
    if (!event.isComposing) renderPage()
    return
  }
  state.forms[node.dataset.bind] = node.type === 'checkbox' ? node.checked : node.value
  if (node.dataset.bind === 'userIncrease') {
    const increaseByMode = state.settings.increase_by_mode || {}
    state.settings.increase_by_mode = {
      ...increaseByMode,
      [state.forms.userMode]: !!state.forms.userIncrease,
    }
  }
  if (node.dataset.bind === 'cookie') {
    const save = document.querySelector('[data-action="cookie-save"]')
    if (save) save.disabled = !String(node.value || '').trim()
  }
})
document.addEventListener('change', event => {
  if (event.target.matches('[data-following-account]')) {
    startFollowingFetch(event.target.value)
    return
  }
  if (event.target.matches('[data-monitor-new-window]')) {
    state.monitorNewWindow = Number(event.target.value) === 7 ? 7 : 1
    saveMonitorColumnPrefs()
    renderPage()
    return
  }
  const node = event.target.closest('[data-bind]')
  if (!node) return
  if (bindSubscriptionField(node)) return
  if (node.dataset.bind === 'settingsSearch') {
    SettingsUI.view.query = node.value
    if (!event.isComposing) renderPage()
    return
  }
  if (node.dataset.bind === 'userMode') {
    const mode = String(node.value || 'post')
    const increaseByMode = state.settings.increase_by_mode || {}
    state.forms.userMode = mode
    state.forms.userIncrease = modeSupportsIncremental(mode)
      && (Object.prototype.hasOwnProperty.call(increaseByMode, mode)
        ? !!increaseByMode[mode]
        : true)
    renderPage()
    return
  }
  if (node.dataset.bind === 'streamerSearch' || node.dataset.bind === 'streamerDate') {
    state.streamer[node.dataset.bind === 'streamerSearch' ? 'search' : 'date'] = node.value
    renderPage()
    return
  }
  state.forms[node.dataset.bind] = node.type === 'checkbox' ? node.checked : node.value
  if (node.dataset.bind === 'userIncrease') {
    const increaseByMode = state.settings.increase_by_mode || {}
    state.settings.increase_by_mode = {
      ...increaseByMode,
      [state.forms.userMode]: !!state.forms.userIncrease,
    }
  }
  if (state.page === 'settings' && node.closest('.settings') && node.dataset.bind !== 'cookie') { applyUiPreferences(); scheduleSettingsSave(node.type === 'radio' ? 0 : 250) }
  if (node.type === 'radio') renderPage()
})
document.addEventListener('toggle', event => {
  const category = event.target.dataset.settingsExpander
  if (category) {
    if (event.target.open) SettingsUI.view.expanded.add(category)
    else SettingsUI.view.expanded.delete(category)
  }
}, true)
document.addEventListener('compositionend', event => {
  if (event.target.matches('[data-bind="settingsSearch"]')) { SettingsUI.view.query = event.target.value; renderSettingsResults(); return }

  const node = event.target.closest('[data-bind="streamerSearch"]')
  if (!node) return
  state.streamer.search = node.value
  if (state.page === 'streamer') renderPage()
})
document.addEventListener('error', async event => {
  const node = event.target
  if (!(node instanceof Element)) return
  if (node.matches('.streamer-card-media img, .streamer-card-media video, .GGxeUe0C img, .wzoPLIem img')) {
    node.alt = '封面暂不可用，打开作品重试'
    node.removeAttribute('src')
  } else if (node.matches('.streamer-modal-media, .streamer-modal-audio, #streamer-media-video, #streamer-media-audio')) {
    const modal = state.streamer.modal
    const work = streamerCurrentWork()
    if (!node.isConnected || !modal || !work) return
    if (work && work.media_type !== 'image') {
      const nextMediaIndex = Number(modal.mediaIndex || 0) + 1
      if (streamerMediaSource(work, nextMediaIndex)) {
        modal.mediaIndex = nextMediaIndex
        modal.playing = false
        renderStreamerOverlay()
        return
      }
    }
    work.media_error = '媒体加载失败'
    if (!modal.fallbackAttempted) {
      await openStreamerWork(modal.workIndex, true)
    } else {
      modal.error = work.media_error
      renderStreamerOverlay()
    }
  }
}, true)
document.addEventListener('wheel', async event => {
  if (state.page !== 'streamer' || !state.streamer.modal) return
  const modal = event.target.closest ? event.target.closest('#streamer-modal') : null
  if (!modal || Math.abs(Number(event.deltaY || 0)) < 2) return
  if (event.target.closest && event.target.closest('.streamer-related-panel, .cv-comments')) return
  event.preventDefault()
  if (streamerWheelLock) return
  const delta = event.deltaY > 0 ? 1 : -1
  streamerWheelLock = true
  try { await moveStreamerWork(delta) }
  finally { window.setTimeout(() => { streamerWheelLock = false }, 420) }
}, { passive: false })
document.addEventListener('toggle', event => { if (event.target.matches('[data-cookie-details]')) state.forms.cookieOpen = event.target.open }, true)
function moveMonitorColumnLive(target, before) {
  const source = draggingMonitorColumn
  const placeholder = draggingMonitorPlaceholder
  const list = target && target.parentElement
  if (!source || !placeholder || !list || placeholder.parentElement !== list || target.dataset.column === source) return false

  const insertionPoint = before ? target : target.nextElementSibling
  if (insertionPoint === placeholder || (before && placeholder.nextElementSibling === target) || (!before && target.nextElementSibling === placeholder)) return false
  const next = state.monitorColumns.slice()
  const sourceIndex = next.indexOf(source)
  const targetIndex = next.indexOf(target.dataset.column)
  if (sourceIndex < 0 || targetIndex < 0) return false
  next.splice(sourceIndex, 1)
  next.splice(next.indexOf(target.dataset.column) + (before ? 0 : 1), 0, source)

  const previousTop = new Map([...list.querySelectorAll('[data-column-row]')].map(node => [node, node.getBoundingClientRect().top]))
  list.insertBefore(placeholder, insertionPoint)
  state.monitorColumns = next
  list.querySelectorAll('[data-column-row]').forEach(node => {
    if (!node.animate) return
    const previous = previousTop.get(node)
    if (previous == null) return
    const delta = previous - node.getBoundingClientRect().top
    if (Math.abs(delta) < 0.5) return
    node.animate([{ transform: `translateY(${delta}px)` }, { transform: 'translateY(0)' }], { duration: 140, easing: 'ease-out' })
  })
  return true
}
function finishMonitorPointerDrag(cancelled) {
  const row = draggingMonitorRow
  if (!row) return
  if (draggingMonitorPointerId != null && row.hasPointerCapture && row.hasPointerCapture(draggingMonitorPointerId)) {
    row.releasePointerCapture(draggingMonitorPointerId)
  }
  if (cancelled && draggingMonitorOrder) state.monitorColumns = draggingMonitorOrder.slice()
  if (draggingMonitorPlaceholder && draggingMonitorPlaceholder.parentElement) {
    draggingMonitorPlaceholder.parentElement.insertBefore(row, draggingMonitorPlaceholder)
    draggingMonitorPlaceholder.remove()
  }
  row.classList.remove('dragging')
  row.removeAttribute('aria-grabbed')
  row.setAttribute('style', draggingMonitorRowStyle)
  draggingMonitorColumn = null
  draggingMonitorRow = null
  draggingMonitorOrder = null
  draggingMonitorPointerId = null
  draggingMonitorPlaceholder = null
  draggingMonitorRowStyle = ''
  draggingMonitorOffsetX = 0
  draggingMonitorOffsetY = 0
  if (!cancelled) saveMonitorColumnPrefs()
  renderPage()
}
document.addEventListener('pointerdown', event => {
  const handle = event.target.closest('.column-drag-handle')
  const row = handle && handle.closest('[data-column-row]')
  if (!row || !state.columnPickerOpen || event.button !== 0) return
  event.preventDefault()
  draggingMonitorColumn = row.dataset.column
  draggingMonitorRow = row
  draggingMonitorOrder = state.monitorColumns.slice()
  draggingMonitorPointerId = event.pointerId
  draggingMonitorRowStyle = row.getAttribute('style') || ''
  const rect = row.getBoundingClientRect()
  draggingMonitorOffsetX = event.clientX - rect.left
  draggingMonitorOffsetY = event.clientY - rect.top
  draggingMonitorPlaceholder = document.createElement('div')
  draggingMonitorPlaceholder.className = 'column-picker-row-placeholder'
  draggingMonitorPlaceholder.style.height = `${rect.height}px`
  draggingMonitorPlaceholder.style.width = `${rect.width}px`
  row.parentElement.insertBefore(draggingMonitorPlaceholder, row)
  document.body.appendChild(row)
  row.style.position = 'fixed'
  row.style.left = `${rect.left}px`
  row.style.top = `${rect.top}px`
  row.style.width = `${rect.width}px`
  row.style.height = `${rect.height}px`
  row.style.zIndex = 'var(--layer-menu)'
  row.classList.add('dragging')
  row.setAttribute('aria-grabbed', 'true')
  if (row.setPointerCapture) row.setPointerCapture(event.pointerId)
})
document.addEventListener('pointermove', event => {
  if (!draggingMonitorRow || event.pointerId !== draggingMonitorPointerId) return
  event.preventDefault()
  draggingMonitorRow.style.left = `${event.clientX - draggingMonitorOffsetX}px`
  draggingMonitorRow.style.top = `${event.clientY - draggingMonitorOffsetY}px`
  const node = document.elementFromPoint(event.clientX, event.clientY)
  const row = node && node.closest ? node.closest('[data-column-row]') : null
  if (!row || row === draggingMonitorRow || !draggingMonitorPlaceholder || row.parentElement !== draggingMonitorPlaceholder.parentElement) return
  moveMonitorColumnLive(row, event.clientY < row.getBoundingClientRect().top + row.offsetHeight / 2)
})
document.addEventListener('pointerup', event => {
  if (!draggingMonitorRow || event.pointerId !== draggingMonitorPointerId) return
  finishMonitorPointerDrag(false)
})
document.addEventListener('pointercancel', event => {
  if (!draggingMonitorRow || event.pointerId !== draggingMonitorPointerId) return
  finishMonitorPointerDrag(true)
})
document.addEventListener('keydown', event => {
  if ((event.key === 'Enter' || event.key === ' ') && event.target.closest('[data-action="streamer-date-toggle"], .cv-home [role="tab"][data-action]')) {
    event.preventDefault()
    if (!event.repeat) event.target.click()
    return
  }
  const key = String(event.key || '').toLowerCase()
  const dialog = activeModal()
  const logTab = event.target.closest && event.target.closest('#log-drawer [role="tab"]')
  if (state.logOpen && logTab && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
    const tabs = [...document.querySelectorAll('#log-drawer [role="tab"]')]
    const index = tabs.indexOf(logTab)
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
    setLogTab(tabs[next].dataset.tab)
    tabs[next].focus()
    event.preventDefault()
    return
  }
  if (event.key === 'Tab' && dialog) {
    const focusable = [...dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex="0"]')].filter(node => node.getClientRects().length)
    if (!focusable.length) {
      focusModal(dialog)
      event.preventDefault()
      return
    }
    const index = focusable.indexOf(document.activeElement)
    if (event.shiftKey && (index === 0 || index < 0)) {
      focusable[focusable.length - 1].focus()
      event.preventDefault()
      return
    }
    if (!event.shiftKey && (index === focusable.length - 1 || index < 0)) {
      focusable[0].focus()
      event.preventDefault()
      return
    }
  }
  if (dialog?.classList.contains('image-overlay')) {
    if (event.key === 'Escape') { dialog.querySelector('.image-overlay-close')?.click(); event.preventDefault() }
    return
  }
  if (event.key === 'Escape' && state.monitorAddOpen) {
    closeManualMonitorImport()
    event.preventDefault()
    return
  }
  if (event.key === 'Escape' && state.followingImport.open) {
    const current = state.followingImport
    if (current.phase === 'loading') {
      call('cancel_following_fetch')
    } else if (current.phase === 'importing') {
      current.open = false
      renderPage()
    } else {
      current.open = false
      current.phase = 'idle'
      current.error = ''
      renderPage()
    }
    event.preventDefault()
    return
  }
  if (event.key === 'Escape' && state.qrLogin) {
    closeQrLogin()
    event.preventDefault()
    return
  }
  if (event.key === 'Escape' && state.monitorDelete) {
    if (!state.monitorDelete.busy) {
      state.monitorDelete = null
      renderPage()
      event.preventDefault()
    }
    return
  }
  if (event.key === 'Escape' && (state.subscriptionMenuId || state.subscriptionDelete || state.subscriptionAdd.open)) {
    if (state.subscriptionMenuId) {
      const id = closeSubscriptionMenu()
      document.querySelectorAll('[data-action="subscription-menu-toggle"]').forEach(node => { if (node.dataset.id === id) node.focus({ preventScroll: true }) })
    }
    else if (state.subscriptionDelete) state.subscriptionDelete = null
    else if (!state.subscriptionAdd.busy) state.subscriptionAdd.open = false
    renderPage()
    event.preventDefault()
    return
  }
  if (event.key === 'Escape' && state.columnPickerOpen) {
    state.columnPickerOpen = false
    renderPage()
    document.querySelector('[data-action="toggle-column-picker"]')?.focus({ preventScroll: true })
    event.preventDefault()
    return
  }
  if (event.key === 'Escape' && state.monitorMenuId) {
    const id = closeMonitorRowMenu()
    renderPage()
    const trigger = [...document.querySelectorAll('[data-action="monitor-menu-toggle"]')]
      .find(node => node.dataset.id === id)
    if (trigger) trigger.focus()
    event.preventDefault()
    return
  }
  if ((state.monitorMenuId || state.subscriptionMenuId) && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    const items = [...document.querySelectorAll(`${state.monitorMenuId ? '#monitor-row-menu' : '#subscription-row-menu'} [role="menuitem"]`)]
    const current = items.indexOf(document.activeElement)
    if (items.length) {
      const next = event.key === 'Home' ? 0
        : event.key === 'End' ? items.length - 1
          : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
      items[next].focus()
      event.preventDefault()
    }
    return
  }
  if (state.streamer.modal && !event.target.closest('input, textarea, select, [contenteditable="true"]') && key === 'x') {
    event.preventDefault()
    toggleStreamerComments()
    return
  }
  if (state.streamer.modal && !event.target.closest('input, textarea, select, [contenteditable="true"]') && ['h', 'j', 'k'].includes(key)) {
    const action = key === 'j' ? 'streamer-clear-screen' : key === 'k' ? 'streamer-auto-play' : 'streamer-video-fullscreen'
    const button = document.querySelector(`[data-action="${action}"]`)
    if (button) { event.preventDefault(); button.click(); return }
  }
  if (state.streamer.modal && (event.key === 'ArrowUp' || event.key === 'ArrowDown') && !event.target.closest('input, textarea, select, [contenteditable="true"]')) {
    // 上下键始终切换作品，不让评论面板等可滚动区域吃掉
    event.preventDefault()
    const button = document.querySelector(`[data-action="${event.key === 'ArrowUp' ? 'streamer-work-prev' : 'streamer-work-next'}"]`)
    if (button && !button.disabled && !event.repeat) button.click()
    return
  }
  if (state.streamer.modal && event.key === ' ' && !event.target.closest('input, textarea, select, [contenteditable="true"]')) {
    // 空格始终播放/暂停，不触发当前聚焦按钮，也不滚动评论
    event.preventDefault()
    if (!event.repeat) toggleStreamerPlayback()
    return
  }
  if (state.streamer.modal && (event.key === 'ArrowLeft' || event.key === 'ArrowRight') && !event.target.closest('input, textarea, select, [contenteditable="true"]')) {
    const action = event.key === 'ArrowLeft' ? 'streamer-media-prev' : 'streamer-media-next'
    const button = document.querySelector(`[data-action="${action}"]`)
    if (button && !button.disabled) { event.preventDefault(); button.click(); return }
  }
  if (event.key === 'Escape' && state.streamer.modal) {
    state.streamer.modal = null
    renderPage()
    event.preventDefault()
    return
  }
  if (event.key !== 'Escape' || (!draggingMonitorOrder && !state.columnPickerOpen)) return
  if (draggingMonitorRow) finishMonitorPointerDrag(true)
  if (state.columnPickerOpen) state.columnPickerOpen = false
  renderPage()
})
document.addEventListener('keyup', event => {
  if (state.streamer.modal && event.key === ' ' && !event.target.closest('input, textarea, select, [contenteditable="true"]')) event.preventDefault()
})
function seekStreamerProgress(progressBar, clientX, render = false) {
  const modal = state.streamer.modal
  if (!progressBar || !modal) return
  const player = byId('streamer-media-video') || byId('streamer-media-audio')
  const duration = Number(player && player.duration)
  const rect = progressBar.getBoundingClientRect()
  if (!rect.width) return
  const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width))
  const work = streamerCurrentWork()
  if (work && work.media_type === 'image') {
    const segments = [...progressBar.querySelectorAll('[data-streamer-segment]')]
    if (!segments.length) return
    const position = ratio * segments.length
    const index = Math.min(segments.length - 1, Math.floor(position))
    setStreamerPicture(index, position - index)
  } else {
    if (!Number.isFinite(duration) || duration <= 0) return
    try { player.currentTime = ratio * duration } catch (_) {}
    modal.progress = ratio * 1000
    const played = progressBar.querySelector('.cv-progress-played')
    if (played) played.style.width = `${ratio * 100}%`
  }
  if (render) renderPage()
}
function streamerVolumeValue(value = 1) {
  const number = Number(value)
  return Number.isFinite(number) ? Math.max(0, Math.min(1, number)) : 1
}
function streamerVolumePercent(modal = state.streamer.modal) {
  return Math.round(streamerVolumeValue(modal && modal.volume) * 100)
}
function syncStreamerVolumeControl() {
  const modal = state.streamer.modal
  const volume = document.querySelector('#streamer-modal .cv-volume')
  if (!modal || !volume) return
  const percent = streamerVolumePercent(modal)
  const label = volume.querySelector('.cv-volume-label')
  const drag = volume.querySelector('.cv-volume-fill')
  if (label) label.textContent = String(percent)
  if (drag) drag.style.height = `${percent}%`
}
function seekStreamerVolume(slider, clientY) {
  const modal = state.streamer.modal
  if (!slider || !modal) return
  const bar = slider.querySelector('[data-streamer-volume-bar]')
  const rect = bar && bar.getBoundingClientRect()
  if (!rect || !rect.height) return
  const ratio = Math.max(0, Math.min(1, 1 - (clientY - rect.top) / rect.height))
  modal.volume = ratio
  state.streamer.volume = ratio
  const player = byId('streamer-media-video') || byId('streamer-media-audio')
  if (player) {
    try { player.volume = ratio } catch (_) {}
  }
  syncStreamerVolumeControl()
}
document.addEventListener('pointerdown', event => {
  const progressBar = event.target.closest && event.target.closest('[data-streamer-progress-bar]')
  const volumeSlider = event.target.closest && event.target.closest('[data-streamer-volume-slider]')
  if (volumeSlider && state.streamer.modal) {
    event.preventDefault()
    streamerVolumeDrag = { pointerId: event.pointerId, slider: volumeSlider }
    if (volumeSlider.setPointerCapture) volumeSlider.setPointerCapture(event.pointerId)
    seekStreamerVolume(volumeSlider, event.clientY)
    return
  }
  if (!progressBar || !state.streamer.modal) return
  event.preventDefault()
  streamerProgressDrag = { pointerId: event.pointerId, progressBar }
  if (progressBar.setPointerCapture) progressBar.setPointerCapture(event.pointerId)
  seekStreamerProgress(progressBar, event.clientX)
})
document.addEventListener('pointermove', event => {
  if (streamerVolumeDrag && event.pointerId === streamerVolumeDrag.pointerId) {
    event.preventDefault()
    seekStreamerVolume(streamerVolumeDrag.slider, event.clientY)
    return
  }
  if (!streamerProgressDrag || event.pointerId !== streamerProgressDrag.pointerId) return
  event.preventDefault()
  seekStreamerProgress(streamerProgressDrag.progressBar, event.clientX)
})
document.addEventListener('pointerup', event => {
  if (streamerVolumeDrag && event.pointerId === streamerVolumeDrag.pointerId) {
    if (streamerVolumeDrag.slider.releasePointerCapture) {
      try { streamerVolumeDrag.slider.releasePointerCapture(event.pointerId) } catch (_) {}
    }
    streamerVolumeDrag = null
    return
  }
  if (!streamerProgressDrag || event.pointerId !== streamerProgressDrag.pointerId) return
  if (streamerProgressDrag.progressBar.releasePointerCapture) {
    try { streamerProgressDrag.progressBar.releasePointerCapture(event.pointerId) } catch (_) {}
  }
  streamerProgressDrag = null
})
document.addEventListener('pointercancel', event => {
  if (streamerVolumeDrag && event.pointerId === streamerVolumeDrag.pointerId) {
    streamerVolumeDrag = null
    return
  }
  if (streamerProgressDrag && event.pointerId === streamerProgressDrag.pointerId) streamerProgressDrag = null
})
document.addEventListener('click', event => {
  // A handler that throws used to be indistinguishable from a dead button:
  // nothing happened and the only trace was an uncaught console error (this is
  // how the missing `resetPageScroll` definition shipped).  Surface it.
  dispatchClick(event).catch(error => reportActionError(error, event))
})
document.addEventListener('scroll', event => {
  if (AppUI.consumeRestoredScroll(event.target)) return
  // Browser focus/scroll-into-view can deliver an already-completed scroll
  // after the click. Dismiss only if the anchor actually moved after opening.
  const anchor = (action, id) => [...document.querySelectorAll(`[data-action="${action}"]`)].find(node => node.dataset.id === id)
  if (state.subscriptionMenuId && AppUI.menuAnchorMoved(byId('subscription-row-menu'), anchor('subscription-menu-toggle', state.subscriptionMenuId))) closeSubscriptionMenu()
  if (state.monitorMenuId && AppUI.menuAnchorMoved(byId('monitor-row-menu'), anchor('monitor-menu-toggle', state.monitorMenuId))) closeMonitorRowMenu()
}, true)
window.addEventListener('resize', () => {
  closeSubscriptionMenu()
  if (!state.monitorMenuId) return
  closeMonitorRowMenu()
})
async function dispatchClick(event) {
  if (state.columnPickerOpen && !event.target.closest('[data-column-picker]') && !event.target.closest('[data-action="toggle-column-picker"]')) {
    state.columnPickerOpen = false
    const popup = document.querySelector('[data-column-picker]')
    if (popup) popup.remove()
    const trigger = document.querySelector('[data-action="toggle-column-picker"]')
    if (trigger) trigger.setAttribute('aria-expanded', 'false')
  }
  if (state.monitorMenuId && !event.target.closest('[data-monitor-row-menu]') && !event.target.closest('[data-action="monitor-menu-toggle"]')) {
    closeMonitorRowMenu()
  }
  if (state.subscriptionMenuId && !event.target.closest('[data-subscription-menu]') && !event.target.closest('[data-action="subscription-menu-toggle"]')) {
    closeSubscriptionMenu()
  }
  if (state.subscriptionAdd.open && !state.subscriptionAdd.busy && event.target === document.querySelector('[data-subscription-add-overlay]')) {
    state.subscriptionAdd.open = false
    renderPage()
    return
  }
  if (state.monitorAddOpen && event.target === document.querySelector('[data-monitor-add-overlay]')) {
    closeManualMonitorImport()
    return
  }
  if (state.monitorDelete && event.target === document.querySelector('[data-monitor-delete-overlay]')) {
    if (!state.monitorDelete.busy) {
      state.monitorDelete = null
      renderPage()
    }
    return
  }
  const segment = event.target.closest('[data-streamer-segment]')
  if (segment && state.streamer.modal) {
    const work = streamerCurrentWork()
    const total = Array.isArray(work && work.media_sources) ? work.media_sources.length : 0
    const index = Number(segment.dataset.streamerSegment)
    if (work && work.media_type === 'image' && total && Number.isInteger(index) && index >= 0 && index < total) {
      const rect = segment.getBoundingClientRect()
      const local = rect.width > 0 ? Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) : 0
      setStreamerPicture(index, local)
    }
    return
  }
  const progressBar = event.target.closest('[data-streamer-progress-bar]')
  if (progressBar && state.streamer.modal) {
    seekStreamerProgress(progressBar, event.clientX, true)
    return
  }
  if (event.target.closest('[data-streamer-volume-slider]')) return
  const liveLink = event.target.closest('.cv-home a[href], #streamer-modal a[href]')
  if (liveLink && !liveLink.dataset.action) {
    event.preventDefault()
  }
  const surface = event.target.closest('#streamer-modal')
  if (surface && state.streamer.modal && !event.target.closest('.cv-controls, .cv-progress, .cv-rail, .cv-work-switch, .cv-caption, .streamer-related-panel, .cv-comments, button, input, select, textarea, [contenteditable="true"], [data-action]')) {
    toggleStreamerPlayback()
    return
  }
  const node = event.target.closest('[data-action]'); if (!node) return
  const action = node.dataset.action; const scope = node.dataset.scope; const id = node.dataset.id
  if (action.startsWith('work-details-')) { await WorkDetails.handle(action, node); return }
  if (action.startsWith('subscription-') && await handleSubscriptionAction(action, node)) return
  if (action === 'user-home') { await openAccountHome(); return }
  if (action === 'user-mode') {
    const mode = node.dataset.mode
    if (!state.userAccount || !modeLabels[mode]) return
    if (!state.userAccount.is_self && ['collect', 'collectmix'].includes(mode)) return
    state.forms.userMode = mode
    if (!modeSupportsIncremental(mode)) state.forms.userIncrease = false
    state.settings.last_user_account = { sec_uid: state.userAccount.sec_uid,
      is_self: state.userAccount.is_self, mode, viewer: state.userAccount.viewer || '' }
    await call('remember_user_mode', mode)
    renderPage(); return
  }
  if (action === 'account-home-section') {
    state.streamer.homeSection = node.dataset.mode
    renderPage(); return
  }
  if (action === 'account-home-mode') {
    const mode = node.dataset.mode
    if (['post', 'like'].includes(mode)) {
      await call('remember_user_mode', mode)
      state.settings.last_user_account = { sec_uid: state.userAccount.sec_uid,
        is_self: state.userAccount.is_self, mode, viewer: state.userAccount.viewer || '' }
      await openAccountHome(mode); return
    }
    if (!modeLabels[mode]) return
    state.forms.userMode = mode
    if (!modeSupportsIncremental(mode)) state.forms.userIncrease = false
    await call('remember_user_mode', mode)
    state.settings.last_user_account = { sec_uid: state.userAccount.sec_uid,
      is_self: state.userAccount.is_self, mode, viewer: state.userAccount.viewer || '' }
    state.streamer.modal = null
    state.page = 'user'
    render(); return
  }
  if (action === 'account-home-refresh') { await openAccountHome(state.streamer.contentMode || 'like', true); return }
  if (action === 'account-preview-stop') { cancelCurrentAccountPreview(); syncAccountHome([]); return }
  if (action === 'theme-toggle' || action === 'theme-set') {
    await changeTheme(action === 'theme-set' ? node.dataset.themeValue : (document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'))
    return
  }
  if (action === 'sidebar-toggle') { toggleSidebar(); return }
  if (action === 'page') {
    state.page = node.dataset.page; render()
    if (state.page === 'user') await refreshSubscriptions()
    return
  }
  if (action === 'streamer-open') { await openStreamerHome(id, state.page === 'streamer' ? 'following' : state.page); return }
  if (action === 'streamer-more') { await loadMoreStreamerWorks(); return }
  if (action === 'streamer-back') {
    // Land back where the monitor list was, not at its top.
    const returnScroll = Number(state.streamer.returnScroll || 0)
    state.streamer.modal = null
    state.streamer.returnScroll = 0
    state.page = state.streamer.returnPage || 'following'
    render()
    if (returnScroll > 0) window.scrollTo(0, returnScroll)
    return
  }
  if (action === 'streamer-refresh') {
    if (state.streamer.catalogue) { await openAccountHome(state.streamer.contentMode || 'like', true); return }
    if (!state.streamer.secUid || state.streamer.loading || state.streamer.loadingMore) return
    if (state.streamer.subtab === 'hidden') { await loadHiddenStreamerWorks(); return }
    const session = state.streamer
    const previousPage = {
      works: session.works,
      source: session.source,
      hasMore: session.hasMore,
      cursor: session.cursor,
      offset: session.offset,
      mode: session.mode,
      remoteCursors: session.remoteCursors,
      paginationStopped: session.paginationStopped,
      profileError: session.profileError,
    }
    state.streamer.loading = true; state.streamer.loadingMore = false; state.streamer.cursor = 0; state.streamer.offset = 0; state.streamer.mode = 'remote'; state.streamer.remoteCursors = new Set([0]); state.streamer.paginationStopped = false; state.streamer.error = ''; state.streamer.profileError = ''; render()
    const result = await call('creator_home', state.streamer.secUid, true, 0, 0)
    if (state.streamer !== session || state.page !== 'streamer') return
    if (!result || result.error) {
      Object.assign(state.streamer, previousPage, {
        error: result && result.error || '主播资料刷新失败',
      })
    } else {
      const nextMode = result.next_mode === 'remote' ? 'remote' : 'local'
      const nextCursor = Number(result.next_cursor || 0)
      const cursorLoop = !!result.has_more && nextMode === 'remote' && state.streamer.remoteCursors.has(nextCursor)
      const remoteCreator = result.creator && ['remote', 'cached'].includes(result.creator.source)
      Object.assign(state.streamer, {
        creator: remoteCreator ? { ...state.streamer.creator, ...result.creator } : state.streamer.creator,
        works: Array.isArray(result.works) ? result.works : state.streamer.works,
        source: result.source || state.streamer.source,
        hasMore: !cursorLoop && !!result.has_more,
        cursor: nextCursor,
        offset: Number(result.next_offset || 0),
        mode: nextMode,
        profileKnown: remoteCreator ? true : state.streamer.profileKnown,
        paginationStopped: cursorLoop,
        error: cursorLoop ? streamerCursorLoopError : '',
        profileError: result.profile_error || '',
      })
    }
    state.streamer.loading = false; render(); return
  }
  if (action === 'streamer-tab') { state.streamer.tab = node.dataset.tab || 'works'; renderPage(); return }
  if (action === 'streamer-subtab') { await switchStreamerSubtab(node.dataset.tab === 'hidden' ? 'hidden' : 'works'); return }
  if (action === 'streamer-date-toggle') {
    state.streamer.date = state.streamer.date === 'recent' ? '' : 'recent'
    renderPage()
    return
  }
  if (action === 'streamer-open-work') {
    event.preventDefault()
    const workIndex = node.dataset.awemeId
      ? state.streamer.works.findIndex(work => String(work.aweme_id || '') === String(node.dataset.awemeId))
      : Number(node.dataset.index)
    if (!Number.isInteger(workIndex) || !state.streamer.works[workIndex]) return
    await openStreamerWork(workIndex)
    const modal = byId('streamer-modal'); if (modal) modal.focus()
    return
  }
  if (action === 'streamer-modal-close' || action === 'streamer-avatar-home') { state.streamer.modal = null; renderPage(); return }
  if (action === 'streamer-work-prev' || action === 'streamer-work-next') {
    if (!state.streamer.modal) return
    const delta = action === 'streamer-work-next' ? 1 : -1
    await moveStreamerWork(delta); return
  }
  if (action === 'streamer-retry') { if (state.streamer.modal) await openStreamerWork(state.streamer.modal.workIndex, true); return }
  if (action === 'streamer-media-prev' || action === 'streamer-media-next') {
    if (!state.streamer.modal) return
    const work = streamerCurrentWork(); const total = Array.isArray(work && work.media_sources) ? work.media_sources.length : 0
    const delta = action === 'streamer-media-next' ? 1 : -1
    const next = Number(state.streamer.modal.mediaIndex || 0) + delta
    if (!total || next < 0 || next >= total) return
    setStreamerPicture(next); return
  }
  if (action === 'streamer-interaction') { showToast('本地浏览不支持点赞、评论等互动'); return }
  if (action === 'streamer-related-toggle') { if (state.streamer.modal) { state.streamer.modal.relatedOpen = !state.streamer.modal.relatedOpen; if (state.streamer.modal.relatedOpen) state.streamer.commentsOpen = false; renderPage() } return }
  if (action === 'streamer-comments-toggle') { toggleStreamerComments(); return }
  if (action === 'streamer-like' || action === 'streamer-collect') { toggleStreamerReaction(action === 'streamer-like' ? 'like' : 'collect', node); return }
  if (action === 'streamer-comments-retry') {
    const comments = state.streamer.comments
    if (comments) { comments.error = ''; comments.author.error = '' }
    loadStreamerComments(true)
    return
  }
  if (action === 'streamer-comments-tab') { setStreamerCommentsTab(node.dataset.tab || 'all'); return }  if (action === 'streamer-replies-more') { loadStreamerReplies(node.dataset.cid || ''); return }
  if (action === 'streamer-replies-collapse') {
    const replies = state.streamer.comments && state.streamer.comments.replies[node.dataset.cid || '']
    if (replies) { replies.open = false; syncStreamerComments() }
    return
  }
  if (action === 'streamer-comment-image') {
    event.preventDefault()
    let sources = []
    try { sources = JSON.parse(node.dataset.sources || '[]') } catch (_) {}
    const thumbnail = node.querySelector('img')?.getAttribute('src')
    openCommentImagePreview([...sources, thumbnail], node)
    return
  }
  if (action === 'streamer-media-surface' || action === 'streamer-video-play') { toggleStreamerPlayback(); return }
  if (action === 'streamer-video-volume') { const player = byId('streamer-media-video') || byId('streamer-media-audio'); if (player && state.streamer.modal) { player.muted = !player.muted; state.streamer.modal.muted = player.muted; syncStreamerPlayerControls() } return }
  if (action === 'streamer-clear-screen') { if (state.streamer.modal) { state.streamer.modal.clearScreen = !state.streamer.modal.clearScreen; state.streamer.clearScreen = state.streamer.modal.clearScreen; syncStreamerPlayerControls() } return }
  if (action === 'streamer-auto-play') { if (state.streamer.modal) { state.streamer.modal.autoPlay = !state.streamer.modal.autoPlay; state.streamer.autoPlay = state.streamer.modal.autoPlay; syncStreamerPlayerControls() } return }
  if (action === 'streamer-video-fullscreen') { const player = byId('streamer-media-video'); const modal = byId('streamer-modal'); if (player && player.requestFullscreen) player.requestFullscreen().catch(() => {}); else if (modal && modal.requestFullscreen) modal.requestFullscreen().catch(() => {}); return }
  if (action === 'refresh' && state.page === 'streamer' && state.streamer.catalogue) { await openAccountHome(state.streamer.contentMode || 'like', true); return }
  if (action === 'startup-retry') { await bootstrap(); return }
  if (action === 'refresh') { if (state.page === 'history') await refreshHistory(); else { await call('refresh'); showToast('已刷新') } return }
  if (action === 'toggle-check') { if (state.selected[scope].has(String(id))) state.selected[scope].delete(String(id)); else state.selected[scope].add(String(id)); renderPage(); return }
  if (action === 'select-all') { const list = scope === 'user' ? state.userItems : state.linkItems; const all = list.length && list.every(item => selected(scope, item.aweme_id)); list.forEach(item => all ? state.selected[scope].delete(String(item.aweme_id)) : state.selected[scope].add(String(item.aweme_id))); renderPage(); return }
  if (action === 'link-preview') {
    if (state.busy.link) return
    state.linkItems = []; state.selected.link.clear(); state.busy.link = true; renderStatus(); renderPage()
    const accepted = await call('preview_links', formValues())
    if (accepted !== true) { state.busy.link = false; renderStatus(); renderPage() }
    return
  }
  if (action === 'link-download') { if (state.busy.link || !state.selected.link.size) return; state.busy.link = true; renderStatus(); renderPage(); const accepted = await call('start_download', formValues(), [...state.selected.link]); if (accepted !== true) { state.busy.link = false; renderStatus(); renderPage() } return }
  if (action === 'user-lock-account') { await resolveUserAccount(state.forms.userUrl); return }
  if (action === 'user-current-account') { await resolveUserAccount(); return }
  if (action === 'user-account-change') {
    if (state.busy.user || state.userAccountLoading) return
    const unlocked = await call('unlock_user_account')
    if (unlocked !== true) return
    accountHomeGeneration += 1
    accountHomeSession = null
    state.userTaskProgress.browse = null
    state.userAccount = null
    state.settings.last_user_account = null
    state.settings.pending_user_download = null
    state.forms.userUrl = ''
    state.userItems = []
    state.selected.user.clear()
    renderStatus()
    renderPage()
    return
  }
  if (action === 'user-download') {
    await startAccountDownload({
      url: userRequestUrl(),
      mode: state.forms.userMode,
      limit: Number(state.forms.userLimit || 0),
      increase: modeSupportsIncremental(state.forms.userMode) && !!state.forms.userIncrease,
      path: state.forms.path,
      start_time: state.forms.startTime || '',
      end_time: state.forms.endTime || '',
    })
    return
  }
  if (action === 'pause') { await call('pause'); return }
  if (action === 'resume') { await call('resume'); return }
  if (action === 'stop') { await call('stop'); return }
  if (action === 'monitor-add-open') {
    state.monitorAddOpen = true
    renderPage()
    return
  }
  if (action === 'monitor-add-close') {
    closeManualMonitorImport()
    return
  }
  if (action === 'monitor-add') {
    if (state.monitorAddSubmitting) return
    const raw = state.forms.monitorUrls || ''
    if (!raw.trim()) { showToast('请输入主播主页链接'); return }
    const pendingUrls = addPendingMonitorRows(raw)
    state.monitorAddSubmittedUrls = raw.split(/\r?\n/).map(value => value.trim()).filter(Boolean)
    state.monitorAddFailedUrlsDuringSubmit.clear()
    state.monitorAddSubmitting = true
    renderPage()
    const accepted = await call('add_monitor', raw)
    if (accepted !== true) {
      state.monitorAddSubmitting = false
      state.monitorAddSubmittedUrls = []
      state.monitorAddFailedUrlsDuringSubmit.clear()
      pendingUrls.forEach(removePendingMonitor)
      showToast('主播添加未能启动，链接已保留')
      renderPage()
      return
    }
    const failedUrls = state.monitorAddSubmittedUrls.filter(url => state.monitorAddFailedUrlsDuringSubmit.has(url))
    state.monitorAddSubmitting = false
    state.monitorAddSubmittedUrls = []
    state.monitorAddFailedUrlsDuringSubmit.clear()
    state.forms.monitorUrls = failedUrls.join('\n')
    state.monitorAddOpen = false
    renderPage()
    const rows = await call('monitors')
    if (rows) applyMonitorRows(rows)
    renderPage()
    return
  }
  if (action === 'monitor-refresh') {
    state.errors.monitor = ''
    const rows = await call('monitors')
    if (rows !== null && rows !== undefined) {
      applyMonitorSnapshot(rows)
      if (state.page === 'following') renderPage()
    }
    return
  }
  if (action === 'following-toggle') {
    const current = state.followingImport
    if (current.phase === 'loading') return
    if (current.phase === 'importing') {
      current.open = !current.open
      renderPage()
      return
    }
    if (current.phase === 'ready') {
      current.open = !current.open
      renderPage()
      return
    }
    await startFollowingFetch(current.accountId || '')
    return
  }
  if (action === 'following-cancel') {
    // Close right away; a late result of the cancelled read is ignored.
    const current = state.followingImport
    current.phase = 'idle'; current.open = false; current.requestId = ''; current.error = ''
    setActivity('following', '')
    renderPage()
    await call('cancel_following_fetch')
    return
  }
  if (action === 'following-close') {
    if (state.followingImport.phase === 'loading') return
    if (state.followingImport.phase === 'importing') {
      state.followingImport.open = false
      renderPage()
      return
    }
    state.followingImport.open = false; state.followingImport.phase = 'idle'; renderPage(); return
  }
  if (action === 'following-retry') {
    await startFollowingFetch(state.followingImport.accountId || '')
    return
  }
  if (action === 'following-toggle-row') {
    if (node.disabled) return
    const rowId = String(id || '')
    if (state.followingImport.selected.has(rowId)) state.followingImport.selected.delete(rowId)
    else state.followingImport.selected.add(rowId)
    syncFollowingSelection(); return
  }
  if (action === 'following-select-all') {
    const current = state.followingImport
    const importable = current.items.filter(item => item && item.can_import)
    const all = importable.length > 0 && importable.every(item => current.selected.has(String(item.sec_uid)))
    importable.forEach(item => { const rowId = String(item.sec_uid); if (all) current.selected.delete(rowId); else current.selected.add(rowId) })
    syncFollowingSelection(); return
  }
  if (action === 'following-page-prev' || action === 'following-page-next') {
    const current = state.followingImport
    const maxPage = Math.max(1, Math.ceil(current.items.length / 50))
    current.page = Math.min(maxPage, Math.max(1, Number(current.page || 1) + (action === 'following-page-next' ? 1 : -1)))
    renderPage(); return
  }
  if (action === 'following-import') {
    const current = state.followingImport
    const ids = [...current.selected]
    if (!ids.length || current.phase !== 'ready') return
    if (!current.requestId) { showToast('关注列表请求已失效，请重试'); return }
    current.phase = 'importing'; current.error = ''; renderPage()
    const accepted = await call('import_following', current.requestId, ids)
    if (accepted !== true) { current.phase = 'ready'; showToast('关注导入未能启动'); renderPage() }
    return
  }
  if (action === 'monitor-menu-toggle') {
    if (node.disabled) return
    const opening = state.monitorMenuId !== String(id)
    state.monitorMenuId = opening ? String(id) : ''
    AppUI.setPageMenu(monitorRowMenu())
    alignMonitorRowMenu()
    document.querySelectorAll('[data-action="monitor-menu-toggle"]').forEach(button => button.setAttribute('aria-expanded', String(opening && button.dataset.id === String(id))))
    const target = opening
      ? byId('monitor-row-menu')?.querySelector('[role="menuitem"]')
      : [...document.querySelectorAll('[data-action="monitor-menu-toggle"]')].find(item => item.dataset.id === String(id))
    target?.focus({ preventScroll: true })
    return
  }
  if (action === 'monitor-check-latest' || action === 'monitor-full-scan-one') {
    closeMonitorRowMenu()
    renderPage()
    const latest = action === 'monitor-check-latest'
    const accepted = latest ? await call('check_monitors', [id]) : await requestFullScan([id])
    if (accepted !== true) showToast(latest ? '检查更新未能启动' : '全量扫描未能启动')
    else showToast(latest ? '开始检查更新' : '开始全量扫描')
    return
  }
  if (action === 'monitor-delete-open') { openMonitorDelete([id]); return }
  if (action === 'monitor-delete-local-toggle') {
    if (state.monitorDelete && !state.monitorDelete.busy) state.monitorDelete.deleteLocalWorks = !!node.checked
    return
  }
  if (action === 'monitor-delete-cancel') {
    if (state.monitorDelete && !state.monitorDelete.busy) {
      state.monitorDelete = null
      renderPage()
    }
    return
  }
  if (action === 'monitor-delete-confirm') {
    const request = state.monitorDelete
    if (!request || request.busy) return
    request.busy = true
    request.error = ''
    renderPage()
    const accepted = await call('delete_monitors', request.ids, request.deleteLocalWorks)
    if (accepted !== true) {
      request.busy = false
      request.error = '删除未能启动，请重试'
      renderPage()
      return
    }
    request.ids.forEach(id => state.pendingMonitorDeletes.add(String(id)))
    state.monitorDelete = null
    renderPage()
    showToast('正在删除')
    return
  }
  if (action === 'monitor-select-all') {
    const rows = state.monitors.filter(row => !row._pending)
    const all = rows.length > 0 && rows.every(row => row._selected)
    rows.forEach(row => { row._selected = !all })
    syncMonitorSelection()
    return
  }
  if (action === 'monitor-select') { const row = state.monitors.find(item => String(item.sec_uid) === String(id)); if (row && !row._pending) row._selected = !row._selected; syncMonitorSelection(); return }
  if (action === 'toggle-column-picker') { state.columnPickerOpen = !state.columnPickerOpen; renderPage(); return }
  if (action === 'close-column-picker') { state.columnPickerOpen = false; renderPage(); return }
  if (action === 'toggle-monitor-column') {
    const key = node.dataset.column
    if (!monitorColumnSpecs[key]) return
    const isVisible = !state.hiddenMonitorColumns.has(key)
    if (isVisible && state.monitorColumns.filter(column => !state.hiddenMonitorColumns.has(column)).length <= 1) {
      event.preventDefault()
      showToast('至少保留一列')
      return
    }
    if (isVisible) state.hiddenMonitorColumns.add(key)
    else state.hiddenMonitorColumns.delete(key)
    saveMonitorColumnPrefs()
    renderPage()
    return
  }
  if (action === 'monitor-toggle') {
    const row = state.monitors.find(item => String(item.sec_uid) === String(id))
    if (!row || row._pending) return

    // Update the existing button in place so CSS can animate the thumb and
    // track.  Re-rendering innerHTML here would insert the switch already in
    // its final state and the transition would never be visible.
    const enabled = !Boolean(row.enabled)
    row.enabled = enabled
    pendingMonitorEnabled[String(id)] = enabled
    node.classList.toggle('on', enabled)
    node.setAttribute('aria-pressed', String(enabled))
    const accepted = await call('set_monitor_enabled', [id], enabled)
    if (accepted !== true) {
      delete pendingMonitorEnabled[String(id)]
      row.enabled = !enabled
      node.classList.toggle('on', !enabled)
      node.setAttribute('aria-pressed', String(!enabled))
      if (accepted === false) showToast('无法更新监控状态')
    }
    return
  }
  if (action === 'monitor-comments-toggle') {
    const row = state.monitors.find(item => String(item.sec_uid) === String(id))
    if (!row || row._pending) return
    const enabled = !row.comments_enabled
    row.comments_enabled = enabled
    pendingMonitorComments[String(id)] = enabled
    node.classList.toggle('on', enabled)
    node.setAttribute('aria-pressed', String(enabled))
    const accepted = await call('set_monitor_comments', [id], enabled)
    if (accepted !== true) {
      delete pendingMonitorComments[String(id)]
      row.comments_enabled = !enabled
      node.classList.toggle('on', !enabled)
      node.setAttribute('aria-pressed', String(!enabled))
      if (accepted === false) showToast('无法更新评论保存状态')
    } else if (enabled) showToast('已开启评论保存，正在同步已下载作品的评论')
    return
  }
  if (action === 'monitor-check') { const target = monitorActionTargets(); if (!target.ids.length) { showToast(target.selected ? '所选主播都已暂停监控' : '没有可检查的主播'); return } const accepted = await call('check_monitors', target.ids); if (accepted !== true) showToast('立即检查未能启动'); else showToast(`开始检查：${target.scope}`); return }
  if (action === 'monitor-validate-local') { const target = monitorActionTargets(); const ids = target.selected ? state.monitors.filter(item => item._selected && !item._pending).map(item => item.sec_uid) : []; state.busy.localValidate = true; state.localValidate = { busy: true, scanned: 0, total: 0 }; renderStatus(); renderPage(); const accepted = await call('validate_local_files', ids); if (accepted !== true) { state.busy.localValidate = false; state.localValidate = { busy: false, scanned: 0, total: 0 }; renderStatus(); renderPage(); showToast('校验未能启动'); return } return }
  if (action === 'monitor-full-scan') { const target = monitorActionTargets(); if (!target.ids.length) { showToast(target.selected ? '所选主播都已暂停监控' : '没有可检查的主播'); return } const accepted = await requestFullScan(target.ids); if (accepted !== true) showToast('全量扫描未能启动'); else showToast(`开始全量扫描：${target.scope}`); return }
  if (action === 'monitor-delete') { const ids = state.monitors.filter(item => item._selected && !item._pending && !state.pendingMonitorDeletes.has(String(item.sec_uid))).map(item => item.sec_uid); if (!ids.length) { showToast('请先选择主播'); return } openMonitorDelete(ids); return }
  if (action === 'comment-load') {
    if (state.busy.comments) return
    const aweme = extractCommentAwemeId(state.forms.commentSource)
    if (!aweme) { showToast('请输入作品链接、分享文案或作品 ID'); return }
    state.busy.comments = true; renderStatus(); renderPage()
    const accepted = await call('load_comments', aweme)
    if (accepted !== true) { state.busy.comments = false; renderStatus(); renderPage() }
    return
  }
  if (action === 'image-select-all') { state.images.forEach(image => state.selected.images.add(image.key)); renderPage(); return }
  if (action === 'image-clear') { state.selected.images.clear(); renderPage(); return }
  if (action === 'comment-download') { if (state.busy.comments || !state.selected.images.size) return; state.busy.comments = true; renderStatus(); renderPage(); const accepted = await call('download_comment_images', [...state.selected.images], state.forms.path); if (accepted !== true) { state.busy.comments = false; renderStatus(); renderPage() } return }
  if (action === 'image-zoom') { state.imagePreviewReturnFocus = describeFocus(node); const accepted = await call('zoom_comment_image', node.dataset.key, node.dataset.cid, Number(node.dataset.index)); if (accepted !== true) { state.imagePreviewReturnFocus = null; showToast('原图打开失败') } return }
  if (action === 'history-refresh') { state.history.page = 1; state.errors.history = ''; await refreshHistory(); return }
  if (action === 'history-prev') { state.history.page = Math.max(1, Number(state.history.page || 1) - 1); await refreshHistory(); return }
  if (action === 'history-next') { state.history.page = Number(state.history.page || 1) + 1; await refreshHistory(); return }
  if (action === 'log-toggle') { state.logOpen = !state.logOpen; renderPage(); if (state.logOpen && state.logTab === 'risk') loadRiskReport(); return }
  if (action === 'log-tab') { setLogTab(node.dataset.tab); return }
  if (action === 'risk-refresh') { loadRiskReport(); return }
  if (action === 'close-log') { state.logOpen = false; renderPage(); return }
  if (action === 'toggle-cookie') { state.forms.cookieOpen = !state.forms.cookieOpen; renderPage(); return }
  if (action === 'clear-cookie') { if (!window.confirm('确定退出当前抖音账号吗？本机保存的登录信息会被清除。')) return; state.forms.clearCookie = true; state.forms.cookie = ''; state.settings.cookie_configured = false; state.settings.cookie_invalid = false; state.settings.account_profile = { status: 'idle', nickname: '', avatar_data: '', updated_at: 0 }; renderPage(); await saveSettings(); return }
  if (action === 'profile-refresh') { const accepted = await call('refresh_account_profile'); if (accepted === false) showToast('账号资料刷新未能启动'); return }
  if (action === 'qr-login' || action === 'qr-login-retry') { await startQrLogin(); return }
  if (action === 'account-main') { const accepted = await call('set_main_account', node.dataset.id || ''); showToast(accepted ? '已切换主账号' : '主账号切换失败'); return }
  if (action === 'account-remove') {
    const account = (state.settings.accounts || []).find(item => String(item.sec_uid || '') === String(node.dataset.id || ''))
    const name = account ? account.nickname : '这个账号'
    if (!window.confirm(`确定从账号库移除「${name}」吗？本机保存的这个账号的登录信息会被清除，它的订阅会暂停扫描。`)) return
    const accepted = await call('remove_account', node.dataset.id || '')
    if (!accepted) showToast('移除失败')
    return
  }
  if (action === 'qr-login-window') { await startQrLogin(true); return }
  if (action === 'qr-login-close') { closeQrLogin(); return }
  if (action === 'choose-directory') { const path = await call('choose_directory'); if (path) { state.forms.path = path; renderPage(); await saveSettings() } return }
  if (action === 'open-directory') { await call('open_directory', state.forms.path); return }
  if (action === 'resume-user-download') { closeResumeUserDownloadPrompt(); await restoreLastUserSession(true); return }
  if (action === 'resume-user-later') { closeResumeUserDownloadPrompt(); return }
  if (action === 'resume-user-discard') {
    closeResumeUserDownloadPrompt()
    state.settings.pending_user_download = null
    await call('discard_user_download_checkpoint')
    return
  }
  if (action === 'settings-category') { scrollSettingsTo(node.dataset.category); return }
  if (action === 'settings-retry') { await saveSettings(); return }
  if (action === 'settings-logs' || action === 'settings-risk') {
    state.logOpen = true; setLogTab(action === 'settings-risk' ? 'risk' : 'logs'); renderPage(); return
  }
  if (action === 'cookie-save') { if (String(state.forms.cookie || '').trim()) await saveSettings({ cookie: true }); return }
  if (action === 'setting-switch') {
    const key = node.dataset.key
    const on = !state.forms[key]
    state.forms[key] = on
    node.classList.toggle('on', on)
    node.setAttribute('aria-checked', String(on))
    applyUiPreferences()
    scheduleSettingsSave()
    return
  }
}
window.addEventListener('scroll', () => {
  if (state.page !== 'streamer' || state.streamer.modal || state.streamer.error || !state.streamer.hasMore || state.streamer.loading || state.streamer.loadingMore) return
  const remaining = document.documentElement.scrollHeight - (window.scrollY + window.innerHeight)
  if (remaining < 720) loadMoreStreamerWorks()
}, { passive: true })
window.addEventListener('pywebviewready', bootstrap, { once: true })
window.addEventListener('resize', () => { alignMonitorSwitches(); syncSegmentThumbs(byId('content')); if (state.streamer.modal && streamerCurrentWork()?.media_type === 'image') syncStreamerGallery() })

// Sidebar: collapsed by choice (remembered) or forced by a narrow window.
const SIDEBAR_STORAGE_KEY = 'douyin-downloader-sidebar'
const narrowWindow = window.matchMedia('(max-width: 900px)')
let sidebarCollapsed = (() => { try { return localStorage.getItem(SIDEBAR_STORAGE_KEY) === 'collapsed' } catch { return false } })()
function sidebarIsCollapsed() { return sidebarCollapsed || narrowWindow.matches }
function syncSidebar() {
  const collapsed = sidebarIsCollapsed()
  const focused = document.activeElement
  const focusedTheme = focused && focused.closest('.sidebar-theme, .sidebar-theme-toggle')
  const root = document.documentElement
  if (collapsed) root.dataset.sidebar = 'collapsed'
  else delete root.dataset.sidebar
  const toggle = document.querySelector('.sidebar-toggle')
  if (toggle) {
    toggle.hidden = narrowWindow.matches
    toggle.setAttribute('aria-expanded', String(!collapsed))
    toggle.setAttribute('aria-label', collapsed ? '展开侧边栏' : '收起侧边栏')
    toggle.querySelector('.nav-label').textContent = collapsed ? '展开' : '收起'
  }
  document.querySelectorAll('.nav button').forEach(button => {
    button.setAttribute('aria-label', button.querySelector('.nav-label').textContent)
  })
  hideNavTip()
  // Ctrl+B or a resize can swap the two theme controls while one has focus.
  if (focusedTheme) {
    const target = document.querySelector(collapsed ? '.sidebar-theme-toggle' : '[data-action="theme-set"][aria-pressed="true"]')
    if (target && target !== focused) target.focus({ preventScroll: true })
  } else if (toggle === focused && narrowWindow.matches) {
    document.querySelector('.sidebar-theme-toggle').focus({ preventScroll: true })
  }
}
function toggleSidebar() {
  if (narrowWindow.matches) return
  sidebarCollapsed = !sidebarCollapsed
  try { localStorage.setItem(SIDEBAR_STORAGE_KEY, sidebarCollapsed ? 'collapsed' : 'expanded') } catch {}
  hideNavTip()
  syncSidebar()
}
function navTipTarget(node) {
  return node && node.closest ? node.closest('.sidebar :is(.brand, .nav button, .sidebar-toggle, .sidebar-theme-toggle)') : null
}
function showNavTip(target) {
  const tip = byId('nav-tip')
  if (!tip || !target || !sidebarIsCollapsed()) return
  const label = target.classList.contains('sidebar-toggle')
    ? '展开侧边栏（Ctrl+B）'
    : target.getAttribute('aria-label') || (target.querySelector('.nav-label, .brand > span:not(.brand-mark)') || {}).textContent || ''
  if (!label) return
  const rect = target.getBoundingClientRect()
  hideNavTip()
  target.setAttribute('aria-describedby', 'nav-tip')
  tip.textContent = label
  tip.style.left = `${Math.round(rect.right + 10)}px`
  tip.style.top = `${Math.round(rect.top + rect.height / 2)}px`
  tip.hidden = false
  requestAnimationFrame(() => tip.classList.add('show'))
}
function hideNavTip() {
  const tip = byId('nav-tip')
  if (!tip) return
  document.querySelectorAll('.sidebar [aria-describedby="nav-tip"]').forEach(target => target.removeAttribute('aria-describedby'))
  tip.classList.remove('show')
  tip.hidden = true
}
document.addEventListener('mouseover', event => { const target = navTipTarget(event.target); if (target) showNavTip(target) })
document.addEventListener('mouseout', event => { const target = navTipTarget(event.target); if (target && !target.contains(event.relatedTarget)) hideNavTip() })
document.addEventListener('focusin', event => { const target = navTipTarget(event.target); if (target && target.matches(':focus-visible')) showNavTip(target); else hideNavTip() })
document.addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && String(event.key).toLowerCase() === 'b' && !activeModal()) {
    event.preventDefault()
    toggleSidebar()
  }
})
document.addEventListener('keydown', event => { if (event.key === 'Escape') hideNavTip() })
document.addEventListener('focusout', event => { if (navTipTarget(event.target)) hideNavTip() })
narrowWindow.addEventListener('change', syncSidebar)
syncSidebar()

window.state = state
window.streamerCount = streamerCount
window.streamerDate = streamerDate
window.streamerPublishTime = streamerPublishTime
window.streamerFilteredWorks = streamerFilteredWorks
window.streamerMediaSource = streamerMediaSource
window.streamerMusicSources = streamerMusicSources
window.streamerCurrentWork = streamerCurrentWork
window.streamerRelatedPanel = streamerRelatedPanel
// The shell is ready independently of the native API and database snapshots.
render()
if (bridge()) { syncWindowFrame(); bootstrap() }
