import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { loadIosConfig } from './ios-config.mjs'

const root = fileURLToPath(new URL('..', import.meta.url))
const action = process.argv[2] || 'sync'
if (!['sync', 'open', 'config'].includes(action)) throw new Error('用法：node scripts/ios.mjs [sync|open|config]')
if (action === 'open' && process.platform !== 'darwin') {
  throw new Error('打开或编译 iOS 工程需要安装 Xcode 的 Mac；本机可使用 npm run ios:sync 生成工程资源。')
}
const { env, configFile } = loadIosConfig(root)
console.log(`iOS 配置文件：${configFile}`)
if (action === 'config') process.exit(0)
if (!env.VITE_API_BASE_URL?.trim()) {
  throw new Error(`请在 ${configFile} 中填写 VITE_API_BASE_URL，再构建 iOS 应用。`)
}
const base = new URL(env.VITE_API_BASE_URL.trim().replace(/\/+$/, ''))
if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password
  || base.search || base.hash || base.pathname !== '/') {
  throw new Error('VITE_API_BASE_URL 应为服务根地址，例如 http://localhost:8000 或 https://api.example.com')
}
console.log(`iOS 后端地址：${base.origin}`)
if (['localhost', '127.0.0.1', '[::1]'].includes(base.hostname)) {
  console.log('当前连接本机服务；真机请在上述配置文件填写服务器地址或 Mac 局域网 IP。')
}
function run(binary, args) {
  const result = spawnSync(process.execPath, [fileURLToPath(new URL(binary, import.meta.url)), ...args],
    { cwd: root, stdio: 'inherit', env: { ...process.env, ...env } })
  if (result.error) throw result.error
  if (result.status !== 0) process.exit(result.status || 1)
}
run('../node_modules/vue-tsc/bin/vue-tsc.js', ['--noEmit'])
run('../node_modules/vite/bin/vite.js', ['build', '--mode', 'ios'])
run('../node_modules/@capacitor/cli/bin/capacitor', ['sync', 'ios'])
if (action === 'open') run('../node_modules/@capacitor/cli/bin/capacitor', ['open', 'ios'])
