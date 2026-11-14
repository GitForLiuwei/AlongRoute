import { readFileSync } from 'node:fs'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import ts from 'typescript'

async function urls(base) {
  const source = readFileSync(new URL('../src/lib/serviceUrl.ts', import.meta.url), 'utf8')
    .replace('import.meta.env.VITE_API_BASE_URL', JSON.stringify(base))
  const code = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  }).outputText
  return import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'))
}

test('browser requests remain same-origin', async () => {
  const { serviceUrl } = await urls('')
  assert.equal(serviceUrl('/api/health'), '/api/health')
})

test('iOS API, SSE and map proxy share the backend origin', async () => {
  const { serviceUrl, mapServiceHost } = await urls(' https://api.example.com/// ')
  assert.equal(serviceUrl('/api/places/suggest?q=a%20b'), 'https://api.example.com/api/places/suggest?q=a%20b')
  assert.equal(serviceUrl('/api/search-replay/123/stream'), 'https://api.example.com/api/search-replay/123/stream')
  assert.equal(mapServiceHost(), 'https://api.example.com/_AMapService')
  assert.throws(() => serviceUrl('//unrelated.example/api'))
})

test('backend supports LAN and rejects credentialed or non-root URLs', async () => {
  const { normalizeServiceBase } = await urls('http://192.168.1.5:8000')
  assert.equal(normalizeServiceBase('http://192.168.1.5:8000/'), 'http://192.168.1.5:8000')
  for (const base of ['file:///tmp', 'https://user:pass@example.com', 'https://example.com/api', 'https://example.com?key=x', 'https://example.com/#x']) {
    assert.throws(() => normalizeServiceBase(base))
  }
})
