/* Presentation only. TaskProgress and AccountCatalogue retain their ownership
 * and lifecycle rules; this module never changes queue or download facts. */
(function (global, factory) {
  const api = factory()
  if (typeof module === 'object' && module.exports) module.exports = api
  else global.TaskPresentation = api
})(typeof window === 'undefined' ? globalThis : window, function () {
  const states = {
    unknown: ['读取下载状态…', 'muted'], not_downloaded: ['待下载', 'muted'], queued: ['排队中', 'muted'],
    downloading: ['下载中', 'info'], large: ['下载中', 'info'], extras: ['补封面/配乐', 'info'],
    large_waiting: ['等待大文件', 'muted'], retryable_failed: ['下载失败 · 待重试', 'warning'],
    retrying: ['重试中', 'warning'], permanent_failed: ['无法下载', 'error'], failed: ['下载失败', 'error'],
    downloaded: ['已下载', 'ok'], done: ['已下载', 'ok'], existing: ['已存在', 'ok'], cancelled: ['已取消', 'muted'],
  }
  const aliases = { '待下载': 'not_downloaded', '未下载': 'not_downloaded', '排队': 'queued', '排队中': 'queued',
    '下载中': 'downloading', '失败': 'failed', '已下载': 'downloaded', '已存在': 'existing', '已下载过': 'existing', '已取消': 'cancelled' }
  const labels = Object.fromEntries(Object.entries(states).map(([key, value]) => [key, value[0]]))
  const fileKinds = { media: '媒体文件', cover: '封面', music: '配乐', json: '元数据', avatar: '头像', live: '实况视频' }
  function describe(value) {
    const key = aliases[value] || value || 'not_downloaded'
    const definition = states[key]
    return { key, label: definition ? definition[0] : String(value), tone: definition ? definition[1] : /失败|错误/.test(value) ? 'error' : 'muted' }
  }
  function bytes(value) {
    let size = Number(value)
    if (!Number.isFinite(size) || size <= 0) return '0 B'
    const units = ['B', 'KB', 'MB', 'GB', 'TB']; let unit = 0
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1 }
    return `${size.toFixed(unit === 0 ? 0 : size >= 10 ? 1 : 2)} ${units[unit]}`
  }
  function count(value) {
    const size = Math.max(0, Math.floor(Number(value) || 0))
    if (size >= 100000000) return `${(size / 100000000).toFixed(size >= 1000000000 ? 0 : 1)}亿`
    if (size >= 10000) return `${(size / 10000).toFixed(size >= 10000000 ? 0 : 1)}万`
    return String(size)
  }
  function eta(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return ''
    const total = Math.ceil(seconds), pad = value => String(value).padStart(2, '0')
    return `剩余 ${pad(Math.floor(total / 3600))}:${pad(Math.floor(total / 60) % 60)}:${pad(total % 60)}`
  }
  function progress(done, total) {
    const known = Number.isFinite(Number(total)) && Number(total) > 0
    const received = Math.max(0, Number(done) || 0)
    return { known, done: received, total: known ? Number(total) : 0,
      percent: known ? Math.max(0, Math.min(100, received / Number(total) * 100)) : null }
  }
  return { states, labels, fileKinds, describe, bytes, count, eta, progress }
})
