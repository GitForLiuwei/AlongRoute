import assert from 'node:assert/strict'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { loadIosConfig } from '../scripts/ios-config.mjs'

function fixture(t) {
  const dir = mkdtempSync(join(tmpdir(), 'alongroute-ios-config-'))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const root = join(dir, 'checkout', 'frontend')
  const configDir = join(dir, 'Application Support', 'AlongRoute')
  mkdirSync(root, { recursive: true })
  writeFileSync(join(root, '.env.ios.example'), 'VITE_API_BASE_URL=http://localhost:8000\nVITE_AMAP_JS_KEY=\n')
  return { dir, root, configDir }
}

test('existing Mac settings survive updated and replaced checkouts', t => {
  const { dir, root, configDir } = fixture(t)
  const original = 'VITE_API_BASE_URL=https://api.example.com\nVITE_AMAP_JS_KEY=public-map-key\n'
  writeFileSync(join(root, '.env.ios.local'), original)
  const first = loadIosConfig(root, { configDir })
  assert.equal(readFileSync(first.configFile, 'utf8'), original)

  writeFileSync(join(root, '.env.ios.local'), 'VITE_API_BASE_URL=http://localhost:8000\nVITE_AMAP_JS_KEY=\n')
  assert.equal(loadIosConfig(root, { configDir }).env.VITE_API_BASE_URL, 'https://api.example.com')

  const replacement = join(dir, 'new-checkout', 'frontend')
  mkdirSync(replacement, { recursive: true })
  const loaded = loadIosConfig(replacement, { configDir })
  assert.equal(loaded.env.VITE_API_BASE_URL, 'https://api.example.com')
  assert.equal(loaded.env.VITE_AMAP_JS_KEY, 'public-map-key')
  assert.equal(readFileSync(loaded.configFile, 'utf8'), original)
})

test('first Mac run copies the template only once and honors later profile edits', t => {
  const { root, configDir } = fixture(t)
  const { configFile, env } = loadIosConfig(root, { configDir })
  assert.equal(env.VITE_API_BASE_URL, 'http://localhost:8000')
  writeFileSync(configFile, 'VITE_API_BASE_URL=https://updated.example.com\nVITE_AMAP_JS_KEY=\n')
  writeFileSync(join(root, '.env.local'), 'VITE_AMAP_JS_KEY=old-map-key\n')
  const updated = loadIosConfig(root, { configDir })
  assert.equal(updated.env.VITE_API_BASE_URL, 'https://updated.example.com')
  assert.equal(updated.env.VITE_AMAP_JS_KEY, '')
})

test('explicit shell variables override the saved profile for a temporary build', t => {
  const { root, configDir } = fixture(t)
  const { configFile } = loadIosConfig(root, { configDir })
  const previous = process.env.VITE_API_BASE_URL
  t.after(() => {
    if (previous === undefined) delete process.env.VITE_API_BASE_URL
    else process.env.VITE_API_BASE_URL = previous
  })
  process.env.VITE_API_BASE_URL = 'https://temporary.example.com'
  assert.equal(loadIosConfig(root, { configDir }).env.VITE_API_BASE_URL, 'https://temporary.example.com')
  assert.match(readFileSync(configFile, 'utf8'), /http:\/\/localhost:8000/)
})

test('Linux keeps using checkout configuration without creating a user profile', t => {
  const { root, configDir } = fixture(t)
  writeFileSync(join(root, '.env.ios.local'), 'VITE_API_BASE_URL=https://linux.example.com\n')
  assert.equal(loadIosConfig(root, { configDir: null }).env.VITE_API_BASE_URL, 'https://linux.example.com')
  assert.equal(existsSync(configDir), false)
})
