import type { CapacitorConfig } from '@capacitor/cli'

const config: CapacitorConfig = {
  appId: 'com.alongroute.app',
  appName: '顺路达',
  webDir: 'dist',
  server: { hostname: 'localhost', iosScheme: 'capacitor' },
  ios: { contentInset: 'never', backgroundColor: '#f8fafc' },
}

export default config
