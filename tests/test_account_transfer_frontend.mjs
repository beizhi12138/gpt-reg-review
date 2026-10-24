import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const apiSource = readFileSync(new URL('../webui/frontend/src/api/accountTransfer.js', import.meta.url), 'utf8')
  .replace(/^import .*$/gm, '').replace(/^export /gm, '')

function downloadHarness({ size = 3, failure = false } = {}) {
  const calls = []
  const context = vm.createContext({
    Blob, Uint8Array, atob, ref: value => ({ value }), API_BASE: '',
    localStorage: { getItem: () => null, setItem: () => {} },
    http: {
      get: async path => { calls.push('download'); if (failure) throw new Error('offline'); return new Blob(['x'.repeat(size)]) },
      post: async () => { calls.push('cleanup'); return { deleted_registered: 1, deleted_mailboxes: 0 } },
    },
    URL: { createObjectURL: () => 'blob:fixture', revokeObjectURL: () => {} },
    document: { body: { appendChild: () => {} }, createElement: () => ({ click: () => calls.push('click'), remove: () => {} }) },
    setTimeout: () => {},
  })
  vm.runInContext(apiSource, context)
  return { context, calls }
}

const receipt = { export_id: 'a'.repeat(32), count: 1, size: 3, backup_path: '/fixture/backup', download_url: '/fixture' }

test('verified file download finishes before cleanup', async () => {
  const { context, calls } = downloadHarness()
  await context.downloadAndCleanupTransfer(receipt)
  assert.deepEqual(calls, ['download', 'click', 'cleanup'])
})

for (const config of [{ size: 2 }, { failure: true }]) {
  test(`failed or truncated download never cleans accounts: ${JSON.stringify(config)}`, async () => {
    const { context, calls } = downloadHarness(config)
    await assert.rejects(context.downloadAndCleanupTransfer(receipt))
    assert.deepEqual(calls, ['download'])
    assert.equal(vm.runInContext('lastTransferExport.value.status', context), 'download_failed')
  })
}

test('filtered export collects more than forty pages without truncation', async () => {
  const source = readFileSync(new URL('../webui/frontend/src/views/Registered.vue', import.meta.url), 'utf8')
  const start = source.indexOf('async function collectFilteredEmails(')
  const end = source.indexOf('async function doScopeExport(', start)
  const requested = []
  const count = 25412
  const context = vm.createContext({
    filter: { value: 'all' }, planFilter: { value: 'all' },
    listRegistered: async ({ offset, limit }) => {
      requested.push(offset)
      return { total: count, plan_filter_supported: true,
        items: Array.from({ length: Math.min(limit, count - offset) }, (_, i) => ({ email: `fixture-${offset+i}@example.test` })) }
    },
  })
  vm.runInContext(source.slice(start, end), context)
  const emails = await context.collectFilteredEmails(100000)
  assert.equal(emails.length, count)
  assert.equal(new Set(emails).size, count)
  assert.equal(emails.at(-1), `fixture-${count-1}@example.test`)
  assert.equal(requested.length, 51)
  assert.equal((await context.collectFilteredEmails(1234)).length, 1234)
})
