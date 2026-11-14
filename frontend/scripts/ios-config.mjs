import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { loadEnv } from 'vite'

// Keep Mac settings outside the checkout so replacing the source preserves them.
export function loadIosConfig(root, {
  configDir = process.platform === 'darwin'
    ? join(homedir(), 'Library', 'Application Support', 'AlongRoute') : null,
} = {}) {
  const localFile = join(root, '.env.ios.local')
  if (!configDir) return { env: loadEnv('ios', root, 'VITE_'), configFile: localFile }

  const configFile = join(configDir, '.env.ios.local')
  if (!existsSync(configFile)) {
    mkdirSync(configDir, { recursive: true, mode: 0o700 })
    const source = existsSync(localFile) ? localFile : join(root, '.env.ios.example')
    try {
      writeFileSync(configFile, readFileSync(source), { flag: 'wx', mode: 0o600 })
    } catch (error) {
      // Another build may have initialized the same profile in the meantime.
      if (error.code !== 'EEXIST') throw error
    }
  }
  return {
    env: { ...loadEnv('ios', root, 'VITE_'), ...loadEnv('ios', configDir, 'VITE_') },
    configFile,
  }
}
