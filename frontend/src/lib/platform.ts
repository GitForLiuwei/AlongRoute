import { Capacitor } from '@capacitor/core'
import { Clipboard } from '@capacitor/clipboard'
import { Geolocation } from '@capacitor/geolocation'
import { Share } from '@capacitor/share'

export const isNativeApp = Capacitor.isNativePlatform()

export async function currentCoordinates(): Promise<{ longitude: number; latitude: number }> {
  if (isNativeApp) {
    const position = await Geolocation.getCurrentPosition({ enableHighAccuracy: true, timeout: 15000, maximumAge: 0 })
    return position.coords
  }
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error('当前浏览器不支持定位'))
    navigator.geolocation.getCurrentPosition(position => resolve(position.coords), reject,
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 })
  })
}

export async function copyText(text: string): Promise<void> {
  if (isNativeApp) await Clipboard.write({ string: text })
  else await navigator.clipboard.writeText(text)
}

export async function shareTrip(text: string): Promise<void> {
  await Share.share({ title: '顺路达 · 下车建议', text, dialogTitle: '发送给司机' })
}
