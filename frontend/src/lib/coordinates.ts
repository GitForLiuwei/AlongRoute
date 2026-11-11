import type { Point } from './api'

/** Browser GPS uses WGS-84; requests and AMap markers use GCJ-02. */
export function wgs84ToGcj02(point: Point): Point {
  const { lon, lat } = point
  if (!(lon >= 72.004 && lon <= 137.8347 && lat >= 0.8293 && lat <= 55.8271)) return { ...point }
  const x = lon - 105, y = lat - 35, pi = Math.PI
  const wave = (20 * Math.sin(6 * x * pi) + 20 * Math.sin(2 * x * pi)) * 2 / 3
  let dy = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*Math.sqrt(Math.abs(x)) + wave
  dy += (20*Math.sin(y*pi) + 40*Math.sin(y*pi/3)) * 2/3
  dy += (160*Math.sin(y*pi/12) + 320*Math.sin(y*pi/30)) * 2/3
  let dx = 300 + x + 2*y + .1*x*x + .1*x*y + .1*Math.sqrt(Math.abs(x)) + wave
  dx += (20*Math.sin(x*pi) + 40*Math.sin(x*pi/3)) * 2/3
  dx += (150*Math.sin(x*pi/12) + 300*Math.sin(x*pi/30)) * 2/3
  const rad = lat * pi / 180, magic = 1 - .00669342162296594323 * Math.sin(rad)**2
  dy = dy * 180 / ((6378245 * (1 - .00669342162296594323) / magic**1.5) * pi)
  dx = dx * 180 / ((6378245 / Math.sqrt(magic)) * Math.cos(rad) * pi)
  return { lon: lon + dx, lat: lat + dy }
}
