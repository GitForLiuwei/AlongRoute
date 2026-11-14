import { Capacitor } from '@capacitor/core'
import { Clipboard } from '@capacitor/clipboard'
import { Geolocation } from '@capacitor/geolocation'
import { Share } from '@capacitor/share'

export const isNativeApp = Capacitor.isNativePlatform()

export interface GeoPointLike {
  name: string
  point: { lon: number; lat: number }
}

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

/**
 * 构建高德地图导航 URL：
 * - 在 iOS 原生端返回纯原生 `iosamap://path` URI Scheme（含起点 A、途经下车点 P、司机终点 B），
 *   由 Capacitor WKWebView 的 WebViewDelegationHandler 直接调用 UIApplication.shared.open 唤起高德地图 App；
 * - 在桌面/普通浏览器端返回 Web URI。
 */
export function buildAmapNavigationUrl(
  dropoff: GeoPointLike,
  origin?: GeoPointLike | null,
  driverDestination?: GeoPointLike | null,
  preferNativeScheme: boolean = isNativeApp,
): string {
  const webPoint = (p: GeoPointLike) => `${p.point.lon},${p.point.lat},${encodeURIComponent(p.name)}`
  const webUrl = origin && driverDestination
    ? `https://uri.amap.com/navigation?from=${webPoint(origin)}&to=${webPoint(driverDestination)}&via=${webPoint(dropoff)}&mode=car&policy=0&callnative=1`
    : `https://uri.amap.com/marker?position=${dropoff.point.lon},${dropoff.point.lat}&name=${encodeURIComponent(dropoff.name)}&callnative=1`

  if (!preferNativeScheme) return webUrl

  if (origin && driverDestination) {
    const viaJson = JSON.stringify({
      poiname: dropoff.name,
      lat: dropoff.point.lat,
      lon: dropoff.point.lon,
    })
    return (
      `iosamap://path?sourceApplication=alongroute` +
      `&slat=${origin.point.lat}&slon=${origin.point.lon}&sname=${encodeURIComponent(origin.name)}` +
      `&dlat=${driverDestination.point.lat}&dlon=${driverDestination.point.lon}&dname=${encodeURIComponent(driverDestination.name)}` +
      `&vian=1&viapoints=${encodeURIComponent(viaJson)}` +
      `&dev=0&t=0`
    )
  }

  return (
    `iosamap://navi?sourceApplication=alongroute` +
    `&poiname=${encodeURIComponent(dropoff.name)}` +
    `&lat=${dropoff.point.lat}&lon=${dropoff.point.lon}&dev=0&style=2`
  )
}

export function buildAmapMarkerUrl(place: GeoPointLike, preferNativeScheme: boolean = isNativeApp): string {
  if (preferNativeScheme) {
    return (
      `iosamap://viewMap?sourceApplication=alongroute` +
      `&poiname=${encodeURIComponent(place.name)}` +
      `&lat=${place.point.lat}&lon=${place.point.lon}&dev=0`
    )
  }
  return `https://uri.amap.com/marker?position=${place.point.lon},${place.point.lat}&name=${encodeURIComponent(place.name)}&callnative=1`
}

/**
 * 在 iOS 端直接通过 `iosamap://` 原生协议拉起高德地图 App，不跳转网页版高德导航。
 * (Capacitor 的 WebViewDelegationHandler 会拦截顶层 iosamap:// 导航并直接执行 UIApplication.shared.open)
 */
export function openAmapNavigation(
  dropoff: GeoPointLike,
  origin?: GeoPointLike | null,
  driverDestination?: GeoPointLike | null,
): void {
  if (isNativeApp) {
    const schemeUrl = buildAmapNavigationUrl(dropoff, origin, driverDestination, true)
    window.location.assign(schemeUrl)
    return
  }
  const webUrl = buildAmapNavigationUrl(dropoff, origin, driverDestination, false)
  window.open(webUrl, '_blank', 'noopener,noreferrer')
}

export function openAmapMarker(place: GeoPointLike): void {
  if (isNativeApp) {
    const schemeUrl = buildAmapMarkerUrl(place, true)
    window.location.assign(schemeUrl)
    return
  }
  const webUrl = buildAmapMarkerUrl(place, false)
  window.open(webUrl, '_blank', 'noopener,noreferrer')
}
