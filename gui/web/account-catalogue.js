/* Ordered catalogue state. Download events can never add or move cards. */
;(function (root, factory) {
  const presentation = typeof module === 'object' && module.exports ? require('./task-presentation.js') : root.TaskPresentation
  const api = factory(presentation)
  if (typeof module === 'object' && module.exports) module.exports = api
  if (root) root.AccountCatalogue = api
})(typeof window !== 'undefined' ? window : null, function (presentation) {
  const states = new Set(['unknown', 'not_downloaded', 'queued', 'downloading', 'downloaded', 'failed', 'cancelled'])
  const labels = presentation.labels
  const key = (owner, mode, id) => `${owner}\u0000${mode}\u0000${id}`
  function create(owner, mode, requestId, runtime = new Map()) {
    return { owner, mode, requestId, runtime, works: [], byId: new Map(), local: new Map(), pages: new Map(), nextPage: 0, lastSourceIndex: -1, scanState: 'loading', error: '', hasMore: true }
  }
  function matches(catalogue, event) {
    return !!catalogue && String(event.request_id || '') === catalogue.requestId
      && String(event.sec_uid || '') === catalogue.owner && event.mode === catalogue.mode
  }
  function refresh(catalogue, id) {
    const work = catalogue.byId.get(String(id))
    if (!work) return
    const runtime = catalogue.runtime.get(key(catalogue.owner, catalogue.mode, String(id)))
    work.download_status = runtime ? runtime.state : catalogue.local.get(String(id)) || 'unknown'
    work.last_error = runtime ? runtime.error || '' : ''
  }
  function appendPage(catalogue, event) {
    if (!matches(catalogue, event) || ['cancelled', 'failed', 'done'].includes(catalogue.scanState)) return []
    const index = Number(event.page_index)
    if (!Number.isInteger(index) || index < catalogue.nextPage || catalogue.pages.has(index)) return []
    catalogue.pages.set(index, event)
    const appended = []
    while (catalogue.pages.has(catalogue.nextPage)) {
      const page = catalogue.pages.get(catalogue.nextPage)
      catalogue.pages.delete(catalogue.nextPage)
      catalogue.nextPage += 1
      for (const incoming of Array.isArray(page.works) ? page.works : []) {
        const id = String(incoming && incoming.aweme_id || '')
        if (!id || catalogue.byId.has(id)) continue
        const rank = Number(incoming.source_index)
        if (!Number.isInteger(rank) || rank < 0 || rank <= catalogue.lastSourceIndex) {
          catalogue.scanState = 'failed'
          catalogue.error = '内容顺序校验失败，请刷新主页'
          catalogue.hasMore = false
          return appended
        }
        const work = { ...incoming, source_index: rank, catalogue: true }
        catalogue.lastSourceIndex = rank
        catalogue.byId.set(id, work)
        catalogue.works.push(work)
        refresh(catalogue, id)
        appended.push(work)
      }
      catalogue.hasMore = !!page.has_more
    }
    return appended
  }
  function localStates(catalogue, event) {
    if (!matches(catalogue, event)) return false
    for (const [id, state] of Object.entries(event.states || {})) {
      if (!['downloaded', 'not_downloaded', 'unknown'].includes(state)) continue
      catalogue.local.set(id, state)
      refresh(catalogue, id)
    }
    return true
  }
  function recordDownload(runtime, event) {
    const id = String(event.aweme_id || '')
    const owner = String(event.sec_uid || '')
    const mode = String(event.mode || '')
    const generation = Number(event.generation)
    const sequence = Number(event.sequence)
    if (!id || !owner || !states.has(event.state) || !Number.isSafeInteger(generation) || !Number.isSafeInteger(sequence)) return false
    const entryKey = key(owner, mode, id)
    const previous = runtime.get(entryKey)
    if (previous && (generation < previous.generation || (generation === previous.generation && sequence <= previous.sequence))) return false
    runtime.set(entryKey, { generation, sequence, state: event.state, error: String(event.error || '') })
    return true
  }
  function finish(catalogue, event) {
    if (!matches(catalogue, event)) return false
    if (!['done', 'cancelled', 'failed'].includes(event.state)) return false
    if (['failed', 'cancelled', 'done'].includes(catalogue.scanState)) return false
    catalogue.scanState = event.state
    catalogue.error = String(event.error || '')
    if (event.state === 'done' && catalogue.pages.size) {
      catalogue.scanState = 'failed'
      catalogue.error = '分页事件不完整，请刷新主页'
    }
    catalogue.hasMore = false
    return true
  }
  function appendCards(anchor, works, renderCard) {
    if (!anchor || !anchor.parentElement) return 0
    const present = new Set(Array.from(anchor.parentElement.querySelectorAll('[data-catalogue-card]'), node => node.getAttribute('data-catalogue-card')))
    const html = []
    for (const work of works) {
      const id = String(work.aweme_id || '')
      if (!id || present.has(id)) continue
      present.add(id)
      html.push(renderCard(work))
    }
    if (html.length) anchor.insertAdjacentHTML('beforebegin', html.join(''))
    return html.length
  }
  return { create, matches, appendPage, localStates, recordDownload, refresh, finish, appendCards, labels }
})
