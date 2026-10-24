/* The editor consumes the Python-generated schema and the existing bridge. */
const SettingsUI = (() => {
  let definition = window.SettingsSchema
  const view = { group: 'account', query: '', expanded: new Set() }
  const fields = () => definition.fields
  function setSchema(value) { if (value?.version === 1) definition = value }
  function defaults() { return Object.fromEntries(fields().map(field => [field.form, field.default])) }
  function values(forms) { return Object.fromEntries(fields().map(field => [field.form, forms[field.form] ?? field.default])) }
  function applySaved(saved, forms) {
    setSchema(saved.settings_schema)
    for (const field of fields()) forms[field.form] = saved[field.state || field.key] ?? field.default
  }
  function matches(field, query) {
    return !query || [field.title, field.description, field.key, field.form,
      ...(field.options || []).flat()].join(' ').toLocaleLowerCase().includes(query.toLocaleLowerCase())
  }
  function validate(forms) {
    const errors = {}
    for (const field of fields()) {
      const value = forms[field.form]
      if (field.type === 'number' && (!Number.isInteger(Number(value)) || value === '' ||
          Number(value) < field.minimum || Number(value) > field.maximum)) {
        errors[field.form] = `请输入 ${field.minimum}–${field.maximum} 之间的整数`
      }
      if (field.type === 'template') {
        const names = [...String(value).matchAll(/\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g)].map(match => match[1])
        if (!names.includes('id')) errors[field.form] = '模板必须包含 {id}，以免作品互相覆盖'
        else if (/[\/\\]/.test(value) || String(value).length > definition.template_max_length) errors[field.form] = `模板不能含路径分隔符，且不能超过 ${definition.template_max_length} 个字符`
        else if (names.some(name => !definition.template_variables.includes(name))) errors[field.form] = '模板包含未知变量'
      }
      if (field.type === 'proxy' && value) {
        try {
          const url = new URL(value)
          if (!['http:', 'https:', 'socks5:', 'socks5h:'].includes(url.protocol) || !url.hostname ||
              (!url.port && !/:(80|443)(?:\/|$)/.test(value))) throw new Error()
        } catch { errors[field.form] = '请输入带端口的 http、https、socks5 或 socks5h URL' }
      }
    }
    return errors
  }
  return { fields, setSchema, defaults, values, applySaved, matches, validate, view, groups: () => definition.groups }
})()
window.SettingsUI = SettingsUI
let settingsSaveTimer = 0
let settingsSaving = null
let settingsSaveState = 'saved'
let settingsSaveError = ''
function settingsSnapshot() { return JSON.stringify(SettingsUI.values(state.forms)) }
function settingsDirty() { return !!state.settingsSnapshot && state.settingsSnapshot !== settingsSnapshot() }
function syncSettingsSaveStatus() {
  const node = byId('settings-save-status')
  if (!node) return
  node.dataset.state = settingsSaveState
  node.textContent = settingsSaveState === 'saving' ? '正在保存…' : settingsSaveState === 'error'
    ? settingsSaveError : settingsDirty() ? '有未保存的更改' : '所有更改已保存'
  const retry = document.querySelector('[data-action="settings-retry"]')
  if (retry) retry.hidden = settingsSaveState !== 'error'
  const errors = SettingsUI.validate(state.forms)
  for (const field of SettingsUI.fields()) {
    const input = document.querySelector(`[data-setting="${field.form}"] input`)
    const help = byId(`setting-${field.form}-help`)
    if (!input || !help) continue
    if (errors[field.form]) input.setAttribute('aria-invalid', 'true')
    else input.removeAttribute('aria-invalid')
    let error = help.querySelector('.setting-error')
    if (errors[field.form]) {
      if (!error) { error = document.createElement('span'); error.className = 'setting-error'; error.setAttribute('role', 'alert'); help.append(error) }
      error.textContent = errors[field.form]
    } else error?.remove()
  }
}
function applyUiPreferences() {
  state.theme = state.forms.theme
  applyTheme(state.theme)
  AppPreferences.apply(state.forms)
}
function scheduleSettingsSave(delay = 250) {
  clearTimeout(settingsSaveTimer)
  syncSettingsSaveStatus()
  settingsSaveTimer = setTimeout(() => { saveSettings() }, delay)
}
async function saveSettings({ cookie = false } = {}) {
  clearTimeout(settingsSaveTimer)
  if (settingsSaving) { await settingsSaving; return saveSettings({ cookie }) }
  if (!cookie && !settingsDirty() && !state.forms.clearCookie) return
  const errors = SettingsUI.validate(state.forms)
  if (Object.keys(errors).length) {
    settingsSaveState = 'error'; settingsSaveError = '未保存：' + Object.values(errors)[0]
    syncSettingsSaveStatus(); return
  }
  const submitted = SettingsUI.values(state.forms)
  const submittedCookie = state.forms.cookie
  const payload = { ...submitted, cookie: cookie ? submittedCookie : '', clear_cookie: !!state.forms.clearCookie }
  settingsSaveState = 'saving'; settingsSaveError = ''; syncSettingsSaveStatus()
  settingsSaving = call('save_settings', payload)
  const saved = await settingsSaving
  settingsSaving = null
  if (!saved) {
    settingsSaveState = 'error'; settingsSaveError = '更改尚未保存，请检查输入或重试'
    syncSettingsSaveStatus(); return
  }
  // An edit made while the bridge was saving must survive its older response.
  const latest = SettingsUI.values(state.forms)
  const cookieDraft = state.forms.cookie, cookieOpen = state.forms.cookieOpen
  applySettingsState(saved)
  for (const [key, value] of Object.entries(latest)) {
    if (value !== submitted[key]) state.forms[key] = value
  }
  state.forms.cookieOpen = cookieOpen
  state.forms.cookie = cookie && cookieDraft === submittedCookie ? '' : cookieDraft
  if (payload.clear_cookie) state.forms.clearCookie = false
  applyUiPreferences()
  settingsSaveState = 'saved'
  if (cookie) { showToast('Cookie 已保存'); if (state.page === 'settings') renderPage() }
  syncSettingsSaveStatus()
  if (settingsDirty() || state.forms.clearCookie) scheduleSettingsSave()
}
function receiveSettings(saved) {
  if (settingsSaving || settingsDirty()) {
    state.settings = { ...state.settings, ...saved }
    return
  }
  const cookieDraft = state.forms.cookie, cookieOpen = state.forms.cookieOpen
  applySettingsState(saved)
  state.forms.cookie = cookieDraft; state.forms.cookieOpen = cookieOpen
  syncSettingsSaveStatus()
}
function renderSettingsResults() {
  const results = document.querySelector('.settings-layout > .settings')
  if (!results) return
  const template = document.createElement('template'); template.innerHTML = renderSettings()
  results.replaceChildren(...template.content.querySelector('.settings-layout > .settings').childNodes)
  syncSegmentThumbs(results)
  syncSettingsNav()
}
// Only the settings list scrolls; the nav follows whichever section is at its top.
let settingsScrollTarget = '', settingsScrollTimer = 0
function setSettingsNav(id) {
  SettingsUI.view.group = id
  document.querySelectorAll('.settings-nav [data-category]').forEach(node => {
    if (node.dataset.category === id) node.setAttribute('aria-current', 'page')
    else node.removeAttribute('aria-current')
  })
}
function syncSettingsNav() {
  const list = document.querySelector('.settings-layout > .settings')
  if (!list || settingsScrollTarget) return
  const sections = [...list.querySelectorAll('[data-settings-section]')]
  if (!sections.length) return
  const top = list.getBoundingClientRect().top
  let current = sections[0]
  if (list.scrollTop + list.clientHeight >= list.scrollHeight - 2) current = sections[sections.length - 1]
  else for (const section of sections) if (section.getBoundingClientRect().top - top <= 24) current = section
  setSettingsNav(current.dataset.settingsSection)
}
function scrollSettingsTo(id) {
  const list = document.querySelector('.settings-layout > .settings')
  const section = list && list.querySelector(`[data-settings-section="${id}"]`)
  setSettingsNav(id)
  if (!section) return
  settingsScrollTarget = id
  clearTimeout(settingsScrollTimer)
  settingsScrollTimer = setTimeout(() => { settingsScrollTarget = '' }, 800)
  const reduced = document.documentElement.dataset.motion === 'reduced' || matchMedia('(prefers-reduced-motion: reduce)').matches
  list.scrollTo({ top: list.scrollTop + section.getBoundingClientRect().top - list.getBoundingClientRect().top, behavior: reduced ? 'auto' : 'smooth' })
}
document.addEventListener('scroll', event => {
  if (event.target instanceof Element && event.target.matches('.settings-layout > .settings')) syncSettingsNav()
}, true)
document.addEventListener('scrollend', event => {
  if (event.target instanceof Element && event.target.matches('.settings-layout > .settings') && settingsScrollTarget) {
    clearTimeout(settingsScrollTimer); settingsScrollTarget = ''
  }
}, true)
function accountProfileCard(profile, loggedInText = '已登录') {
  const data = profile || {}
  const status = String(data.status || 'idle')
  const nickname = String(data.nickname || '')
  const avatar = data.avatar_data
    ? `<img class="account-avatar" src="${esc(data.avatar_data)}" alt="${esc(nickname || '抖音账号')}">`
    : `<span class="account-avatar account-avatar-fallback">${esc((nickname || '抖').slice(0, 1))}</span>`
  const label = status === 'loading' ? (nickname || '正在读取资料…') : (nickname || '未能读取昵称')
  const sublabel = status === 'error' ? (data.error || '资料读取失败') : loggedInText
  return `<div class="account-profile"><div class="account-avatar-wrap">${avatar}</div><div class="account-profile-copy"><strong>${esc(label)}</strong>${sublabel ? `<span>${esc(sublabel)}</span>` : ''}</div></div>`
}
function accountLibraryRow(account, profile) {
  const id = `data-id="${esc(account.sec_uid || '')}"`
  const expired = !!account.expired
  if (account.is_main) {
    const loading = profile.status === 'loading'
    const card = accountProfileCard({ ...profile, nickname: profile.nickname || account.nickname, avatar_data: profile.avatar_data || account.avatar_data, status: expired ? 'error' : profile.status, error: expired ? '主账号 · 登录已失效' : profile.error }, '主账号 · 用于监控、下载等不分账号的功能')
    const actions = `${button(loading ? '刷新中…' : '刷新资料', 'profile-refresh', 'secondary', loading ? 'disabled' : '', loading)}${button('重新登录', 'qr-login', 'secondary')}${button('移除', 'account-remove', 'secondary', id)}`
    return `<div class="setting setting-account">${card}<div class="setting-control">${actions}</div></div>`
  }
  const card = accountProfileCard({ status: expired ? 'error' : 'ready', nickname: account.nickname, avatar_data: account.avatar_data, error: '登录已失效，它的订阅已暂停' }, '扫描它自己的喜欢、收藏时使用')
  const actions = `${expired ? button('重新登录', 'qr-login', 'primary') : button('设为主账号', 'account-main', 'secondary', id)}${button('移除', 'account-remove', 'secondary', id)}`
  return `<div class="setting setting-account">${card}<div class="setting-control">${actions}</div></div>`
}
function settingRow(title, desc, control, cls = '', titleId = '') {
  return `<div class="setting${cls ? ` ${cls}` : ''}"><div class="setting-copy"><div class="setting-title"${titleId ? ` id="${titleId}"` : ''}>${title}</div>${desc ? `<div class="setting-desc">${desc}</div>` : ''}</div><div class="setting-control">${control}</div></div>`
}
function settingsGroup(title, rows, aside = '', id = '') {
  return `<section class="settings-group"${id ? ` data-settings-section="${id}"` : ''}><div class="settings-group-head"><h2 class="settings-group-title">${title}</h2>${aside}</div><div class="panel settings-card">${rows}</div></section>`
}
function settingSwitch(on, attrs) {
  return `<button type="button" class="switch${on ? ' on' : ''}" role="switch" aria-checked="${!!on}" ${attrs}><i></i></button>`
}
function segmentedGroup(label, name, options, current) {
  return `<div class="segmented" role="radiogroup" aria-label="${esc(label)}"><span class="segment-thumb" aria-hidden="true"></span>${options.map(([value, text]) => `<label class="segment${String(current) === String(value) ? ' checked' : ''}"><input type="radio" name="${name}" data-bind="${name}" value="${esc(value)}"${String(current) === String(value) ? ' checked' : ''}>${text}</label>`).join('')}</div>`
}
const folderIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>'
const openExternalIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>'
function renderSettings() {
  const s = state.settings || {}; const f = state.forms
  const loggedIn = !!s.cookie_configured
  const invalid = !!s.cookie_invalid
  const badgeClass = loggedIn ? 'ok' : (invalid ? 'error' : '')
  const badgeText = loggedIn ? '已登录' : (invalid ? '登录已失效' : '未登录')
  const loginLabel = (loggedIn || invalid) ? '重新登录' : '扫码登录'
  const profile = s.account_profile || {}
  const accounts = Array.isArray(s.accounts) ? s.accounts : []
  const accountRows = accounts.length && (loggedIn || invalid)
    ? accounts.map(account => accountLibraryRow(account, profile)).join('')
      + settingRow('添加账号', '再登录一个抖音账号；订阅它的喜欢、收藏时会用它自己的登录去扫描', button('扫码添加', 'qr-login', 'secondary'))
    : settingRow('抖音账号', invalid ? '登录已失效，重新扫码登录后才能继续下载需要账号的内容' : '登录后才能订阅喜欢、收藏，以及下载需要账号才能看的作品', button(loginLabel, 'qr-login', 'primary'), 'setting-account')
  const cookieRow = settingRow('手动 Cookie', '扫码不方便时，可以粘贴浏览器里复制的 Cookie 添加账号', button(f.cookieOpen ? '收起' : '填写', 'toggle-cookie', 'secondary', 'aria-expanded="' + !!f.cookieOpen + '"'))
  const cookieEditor = f.cookieOpen
    ? `<div class="setting setting-stack cookie-editor-row"><div class="cookie-editor">${input('cookie', f.cookie, '粘贴 Cookie', 'grow')}${button('保存', 'cookie-save', 'primary', String(f.cookie || '').trim() ? '' : 'disabled')}</div></div>`
    : ''
  const accountBadge = accounts.length > 1 ? `${badgeText} · ${accounts.length} 个账号` : badgeText
  const account = settingsGroup('账号', accountRows + cookieRow + cookieEditor, `<span class="status-badge ${badgeClass}">${accountBadge}</span>`, 'account')

  const view = SettingsUI.view
  const query = view.query.trim()
  const errors = SettingsUI.validate(f)
  function fieldRow(field) {
    const name = field.form, value = f[name], id = 'setting-' + name
    const labelled = `aria-labelledby="${id}" aria-describedby="${id}-help"`
    let control
    if (field.type === 'boolean') control = settingSwitch(value, `data-action="setting-switch" data-key="${name}" ${labelled}`)
    else if (field.type === 'choice' && field.options.length <= 4 && name !== 'authorDir') {
      control = segmentedGroup(field.title, name, field.options, value)
    } else if (field.type === 'choice') {
      control = `<select data-bind="${name}" ${labelled}>${field.options.map(([key, label]) =>
        `<option value="${esc(key)}"${String(value) === String(key) ? ' selected' : ''}>${esc(label)}</option>`).join('')}</select>`
    } else {
      control = `<input data-bind="${name}" value="${esc(value)}" ${labelled} ${field.type === 'number'
        ? `type="number" min="${field.minimum}" max="${field.maximum}" step="1"` : 'type="text"'}
        ${errors[name] ? 'aria-invalid="true"' : ''} spellcheck="false" autocomplete="off">`
      if (field.type === 'path') control = `<label class="path-field">${folderIcon}${control}</label>`
        + button('更改', 'choose-directory') + button(openExternalIcon, 'open-directory', 'secondary icon-only',
          'aria-label="在资源管理器中打开" title="在资源管理器中打开"')
    }
    const error = errors[name] ? `<span class="setting-error" role="alert">${esc(errors[name])}</span>` : ''
    return `<div class="setting ${['path', 'template', 'proxy'].includes(field.type) ? 'setting-stack' : ''}" data-setting="${name}">
      <div class="setting-copy"><div class="setting-title" id="${id}">${esc(field.title)}</div>
      <div class="setting-desc" id="${id}-help">${esc(field.description)}${error}</div></div>
      <div class="setting-control">${control}</div></div>`
  }
  const groups = SettingsUI.groups().map(group => {
    if (group.id === 'account') return SettingsUI.matches({ title: '账号 登录 Cookie 主账号 订阅', description: '', key: '', form: '' }, query)
      ? account : ''
    const fields = SettingsUI.fields().filter(field => field.group === group.id && SettingsUI.matches(field, query))
    let extra = group.id === 'advanced' && (!query || /日志|风控|诊断/.test(query))
      ? settingRow('运行诊断', '运行日志和风控统计使用同一个诊断面板。', button('运行日志', 'settings-logs', 'secondary') + button('风控统计', 'settings-risk', 'secondary')) : ''
    if (group.id === 'automation' && (!query || /喜欢|收藏|订阅|账号/.test(query))) extra += settingRow('喜欢与收藏订阅',
      '每个账号使用自己的登录与扫描状态，可在用户作品页管理。', button('管理订阅', 'page', 'secondary', 'data-page="user"'))
    if (!fields.length && !extra) return ''
    const normal = fields.filter(field => !field.advanced).map(fieldRow).join('')
    const advanced = fields.filter(field => field.advanced).map(fieldRow).join('')
    const expanded = query || view.expanded.has(group.id)
    const expandable = advanced ? `<details class="settings-expander" data-settings-expander="${group.id}"${expanded ? ' open' : ''}>
      <summary>命名模板<span>按需调整</span></summary>${advanced}</details>` : ''
    return settingsGroup(group.title, normal + expandable + extra, `<span class="setting-desc">${esc(group.description)}</span>`, group.id)
  }).filter(Boolean)
  const navigation = SettingsUI.groups().map(({ id, title }) => `<button type="button" data-action="settings-category" data-category="${id}" ${view.group === id ? 'aria-current="page"' : ''}>${esc(title)}</button>`).join('')
  return `<div class="settings-workspace"><div class="settings-toolbar">
    <label class="settings-search"><span>搜索设置</span><input type="search" data-bind="settingsSearch" value="${esc(view.query)}"
      placeholder="搜索名称、说明或选项" aria-label="搜索设置" autocomplete="off"></label>
    <output id="settings-save-status" data-state="${settingsSaveState}" role="status">${settingsSaveState === 'error' ? esc(settingsSaveError)
      : settingsSaveState === 'saving' ? '正在保存…' : settingsDirty() ? '有未保存的更改' : '所有更改已保存'}</output>
    ${button('重试保存', 'settings-retry', 'secondary', settingsSaveState === 'error' ? '' : 'hidden')}</div>
    <div class="settings-layout"><nav class="settings-nav" aria-label="设置分类">${navigation}</nav>
    <div class="settings" data-ui-scroll-key="settings">${groups.length ? groups.join('') : emptyState('没有匹配的设置', '尝试其他关键词')}</div></div></div>`
}
