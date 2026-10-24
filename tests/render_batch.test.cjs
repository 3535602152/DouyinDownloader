const assert = require('node:assert/strict')
const test = require('node:test')
const createBatch = require('../gui/web/render-batch.js')
const presentation = require('../gui/web/task-presentation.js')

test('batches preserve all state mutations while painting one projection per frame', () => {
  const frames = [], model = [], paints = []
  const batch = createBatch(fn => frames.push(fn))
  function page() { if (!batch.defer('page', page)) paints.push([...model]) }
  batch.run(() => Array.from({ length: 100 }, (_, i) => { model.push(i); page() }))
  batch.run(() => { model.push('done'); page() })
  assert.equal(frames.length, 1)
  assert.equal(paints.length, 0)
  frames.shift()()
  assert.equal(paints.length, 1)
  assert.deepEqual(paints[0], [...Array(100).keys(), 'done'])
  page()
  assert.equal(paints.length, 2, 'direct user interactions stay immediate')
})

test('a failed event does not leave the renderer locked in a transaction', () => {
  const frames = [], batch = createBatch(fn => frames.push(fn))
  let calls = 0
  function render() { if (!batch.defer('page', render)) calls += 1 }
  assert.throws(() => batch.run(() => { render(); throw Error('bad event') }))
  frames.shift()(); render()
  assert.equal(calls, 2)
})

test('progress is determinate only with a known positive total and is clamped', () => {
  for (const total of [0, null, undefined, -1, Infinity, NaN]) {
    assert.equal(presentation.progress(100, total).percent, null)
  }
  assert.equal(presentation.progress(200, 100).percent, 100)
  assert.equal(presentation.progress(-1, 100).percent, 0)
  assert.equal(presentation.describe('已存在').key, 'existing')
  assert.equal(presentation.describe('retryable_failed').tone, 'warning')
  assert.equal(presentation.describe('failed').tone, 'error')
})
