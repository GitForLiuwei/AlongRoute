import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { loadEnv } from 'vite'

const root = fileURLToPath(new URL('..', import.meta.url))
const action = process.argv[2] || 'sync'
if (!['sync', 'open'].includes(action)) throw new Error('用法：node scripts/ios.mjs [sync|open]')
if (action === 'open' && process.platform !== 'darwin') {
  throw new Error('打开或编译 iOS 工程需要安装 Xcode 的 Mac；本机可使用 npm run ios:sync 生成工程资源。')
}
const env = loadEnv('ios', root, 'VITE_')
if (!env.VITE_API_BASE_URL?.trim()) {
  throw new Error('请复制 .env.ios.example 为 .env.ios.local 并填写 VITE_API_BASE_URL，再构建 iOS 应用。')
}
const base = new URL(env.VITE_API_BASE_URL.trim().replace(/\/+$/, ''))
if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password
  || base.search || base.hash || base.pathname !== '/') {
  throw new Error('VITE_API_BASE_URL 应为服务根地址，例如 http://localhost:8000 或 https://api.example.com')
}
function run(binary, args) {
  const result = spawnSync(process.execPath, [fileURLToPath(new URL(binary, import.meta.url)), ...args],
    { cwd: root, stdio: 'inherit', env: process.env })
  if (result.error) throw result.error
  if (result.status !== 0) process.exit(result.status || 1)
}
run('../node_modules/vue-tsc/bin/vue-tsc.js', ['--noEmit'])
run('../node_modules/vite/bin/vite.js', ['build', '--mode', 'ios'])
run('../node_modules/@capacitor/cli/bin/capacitor', ['sync', 'ios'])
if (action === 'open') run('../node_modules/@capacitor/cli/bin/capacitor', ['open', 'ios'])
