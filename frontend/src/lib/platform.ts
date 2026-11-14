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
 * 构建高德地图导航 URL：在 iOS 原生端返回 `iosamap://path` URI Scheme（含途经点 P），
 * 在 Web 端返回 `https://uri.amap.com/navigation`。
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
 * 在 iOS 端直接拉起高德地图 App 进行路线导航；若设备未安装高德地图 App 则自动回退打开 Web 导航页。
 */
export function openAmapNavigation(
  dropoff: GeoPointLike,
  origin?: GeoPointLike | null,
  driverDestination?: GeoPointLike | null,
): void {
  const webUrl = buildAmapNavigationUrl(dropoff, origin, driverDestination, false)
  if (!isNativeApp) {
    window.open(webUrl, '_blank', 'noopener,noreferrer')
    return
  }
  const schemeUrl = buildAmapNavigationUrl(dropoff, origin, driverDestination, true)
  const startedAt = Date.now()
  window.location.href = schemeUrl
  setTimeout(() => {
    if (!document.hidden && Date.now() - startedAt < 2200) {
      window.open(webUrl, '_blank', 'noopener,noreferrer')
    }
  }, 1500)
}

export function openAmapMarker(place: GeoPointLike): void {
  const webUrl = buildAmapMarkerUrl(place, false)
  if (!isNativeApp) {
    window.open(webUrl, '_blank', 'noopener,noreferrer')
    return
  }
  const schemeUrl = buildAmapMarkerUrl(place, true)
  const startedAt = Date.now()
  window.location.href = schemeUrl
  setTimeout(() => {
    if (!document.hidden && Date.now() - startedAt < 2200) {
      window.open(webUrl, '_blank', 'noopener,noreferrer')
    }
  }, 1500)
}
