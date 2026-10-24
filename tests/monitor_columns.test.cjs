const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')

const app = fs.readFileSync(path.join(__dirname, '../gui/web/app.js'), 'utf8')
const source = app.slice(app.indexOf('const monitorColumnDefaults ='), app.indexOf('const state ='))
const expected = ['monitor', 'nickname', 'status', 'lastCheck', 'hiddenWorks', 'publicCount',
  'localCount', 'last24h', 'storage', 'comments']
const oldColumns = ['monitor', 'nickname', 'status', 'lastCheck', 'latestWork', 'publicCount',
  'last24h', 'localCount', 'storage', 'comments', 'commentsBytes', 'hiddenWorks', 'newWorks',
  'latestPublish', 'idleDays', 'mediaMix', 'videoDuration', 'topDigg', 'avgDigg', 'commentsSaved', 'douyinId']
const oldHidden = ['hiddenWorks', 'newWorks', 'latestPublish', 'idleDays', 'mediaMix',
  'videoDuration', 'topDigg', 'avgDigg', 'commentsSaved', 'douyinId']

function prefs(saved) {
  const context = { localStorage: { getItem: () => saved === undefined ? null : JSON.stringify(saved) } }
  vm.createContext(context)
  vm.runInContext(source + '\nglobalThis.result = readMonitorColumnPrefs()', context)
  const result = context.result
  return { columns: Array.from(result.columns), hidden: Array.from(result.hidden),
    visible: Array.from(result.columns).filter(key => !result.hidden.has(key)), newWindow: result.newWindow }
}

test('new default columns match requested order and visibility', () => {
  const result = prefs()
  assert.deepEqual(result.visible, expected)
  assert.equal(result.columns.length, new Set(result.columns).size)
  assert.equal(result.columns.length, 21)
})

test('untouched previous defaults upgrade while retaining the time window', () => {
  assert.deepEqual(prefs({ columns: oldColumns, hidden: oldHidden, newWindow: 7 }).visible, expected)
  assert.equal(prefs({ columns: oldColumns, hidden: oldHidden, newWindow: 7 }).newWindow, 7)
})

test('custom visibility and ordering are preserved', () => {
  const columns = [...oldColumns]
  ;[columns[0], columns[1]] = [columns[1], columns[0]]
  const hidden = [...oldHidden, 'comments']
  const result = prefs({ columns, hidden })
  assert.deepEqual(result.columns, columns)
  assert.deepEqual(result.hidden, hidden)
  assert.equal(result.visible[0], 'nickname')
  assert.ok(result.visible.includes('latestWork'))
  assert.ok(!result.visible.includes('comments'))
})
