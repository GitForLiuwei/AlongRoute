import type { Point } from './api'

export function validGeoPoint(value: unknown): value is Point {
  if (!value || typeof value !== 'object') return false
  const p = value as Point
  return Number.isFinite(p.lon) && Number.isFinite(p.lat)
    && p.lon >= -180 && p.lon <= 180 && p.lat >= -90 && p.lat <= 90
}

/** Fit geographic coordinates with one Mercator scale, without stretching either axis.
 * Input coordinates stay in the API's coordinate system; no GPS conversion is applied.
 */
export function createGeoProjector(points: Point[], width = 800, height = 580, padding = 65) {
  const valid = points.filter(validGeoPoint)
  if (!valid.length) return (_point: Point): [number, number] => [width / 2, height / 2]
  const anchor = valid[0].lon
  const mercator = (p: Point): [number, number] => {
    // Keep routes crossing the date line together instead of spanning the globe.
    const delta = ((p.lon - anchor + 540) % 360) - 180
    const lat = Math.max(-85.05112878, Math.min(85.05112878, p.lat)) * Math.PI / 180
    return [(anchor + delta) * Math.PI / 180, Math.log(Math.tan(Math.PI / 4 + lat / 2))]
  }
  const projected = valid.map(mercator)
  const xs = projected.map(p => p[0]), ys = projected.map(p => p[1])
  const minX = xs.length ? Math.min(...xs) : 0, maxX = xs.length ? Math.max(...xs) : 0
  const minY = ys.length ? Math.min(...ys) : 0, maxY = ys.length ? Math.max(...ys) : 0
  // A single point or identical coordinates use a small, finite geographic extent.
  const spanX = Math.max(maxX - minX, 1e-6), spanY = Math.max(maxY - minY, 1e-6)
  const scale = Math.min(Math.max(1, width - 2 * padding) / spanX, Math.max(1, height - 2 * padding) / spanY)
  const centerX = (minX + maxX) / 2, centerY = (minY + maxY) / 2
  return (point: Point): [number, number] => {
    if (!validGeoPoint(point)) return [width / 2, height / 2]
    const [x, y] = mercator(point)
    return [width / 2 + (x - centerX) * scale, height / 2 - (y - centerY) * scale]
  }
}
