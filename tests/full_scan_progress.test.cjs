'use strict'
const assert = require('node:assert/strict')
const progress = require('../gui/web/task-progress.js')

const initial = {
  task_id: 1, revision: 1, status: 'scanning', scan_total: 3, scan_done: 1,
  scan_failed: 0, download_total: 2, download_done: 1, download_success: 1,
  download_failed: 0, current_creator_name: 'Creator', current_scanned: 4,
}
const current = progress.accept(null, initial)
assert.deepEqual(current, initial)
for (const change of [
  { revision: 1, scan_done: 2 },
  { revision: 2, scan_done: 0 },
  { revision: 2, scan_done: 4 },
  { revision: 2, download_done: 0, download_success: 0 },
  { revision: 2, download_done: 2 },
  { task_id: 0, revision: 2 },
]) assert.equal(progress.accept(current, { ...initial, ...change }), current)

const done = progress.accept(current, {
  ...initial, revision: 2, scan_done: 3, scan_failed: 1, status: 'done',
  download_done: 2, download_failed: 1,
})
assert.equal(progress.active(done), false)
assert.equal(progress.format(done).scanTone, 'bad')
assert.equal(progress.format(done).download, '下载 2 / 2')
assert.equal(progress.accept(done, { ...initial, task_id: 2 }).task_id, 2)
assert.equal(progress.accept(done, { ...initial, revision: 99 }), done)
assert.equal(progress.format({ ...initial, status: 'interrupted', error: 'Stopped' }).detail, 'Stopped')
console.log('Full-scan progress invariants passed')
