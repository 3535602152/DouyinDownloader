/* Events mutate state in arrival order; DOM projections render once per frame. */
(function (global, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory
  else global.AppRenderBatch = factory(callback => global.requestAnimationFrame(callback))
})(typeof window === 'undefined' ? globalThis : window, function (scheduleFrame) {
  const pending = new Map()
  let depth = 0, scheduled = false, flushing = false
  function flush() {
    scheduled = false
    const jobs = [...pending.values()]; pending.clear()
    flushing = true
    try { jobs.forEach(job => job()) } finally { flushing = false }
  }
  function defer(key, render) {
    if (flushing || (!depth && !pending.size)) return false
    pending.set(key, render)
    return true
  }
  function run(mutate) {
    depth += 1
    try { return mutate() } finally {
      depth -= 1
      if (!depth && pending.size && !scheduled) { scheduled = true; scheduleFrame(flush) }
    }
  }
  return { run, defer }
})
