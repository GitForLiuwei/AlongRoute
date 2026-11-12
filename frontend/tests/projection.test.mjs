import { readFileSync } from 'node:fs'
import assert from 'node:assert/strict'
import { test } from 'node:test'
import ts from 'typescript'

// Run the real TypeScript helpers with the project's existing compiler.
const compile = source => 'data:text/javascript;base64,' + Buffer.from(ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText).toString('base64')
const geoUrl = compile(readFileSync(new URL('../src/lib/geoProjector.ts', import.meta.url), 'utf8'))
const { createGeoProjector, validGeoPoint } = await import(geoUrl)
const { projectionCandidates } = await import(compile(readFileSync(new URL('../src/lib/candidateProjection.ts', import.meta.url), 'utf8')
  .replace("'./geoProjector'", JSON.stringify(geoUrl))))
const point = (lon, lat) => ({ lon, lat })

test('one scale preserves local east/north distances instead of stretching axes', () => {
  const a = point(113, 23), east = point(113.01, 23), north = point(113, 23 + .01 * Math.cos(23 * Math.PI / 180))
  const project = createGeoProjector([a, east, north], 1000, 300)
  const [x, y] = project(a), [ex] = project(east), [, ny] = project(north)
  assert.ok(Math.abs((ex - x) / (y - ny) - 1) < .001)
  assert.ok(ex > x && ny < y)
})

test('fits tall, wide, negative and southern coordinates within padding', () => {
  for (const points of [[point(1, 1), point(1.001, 50)], [point(-100, -20), point(100, -21)]]) {
    const project = createGeoProjector(points, 800, 580, 65)
    for (const p of points) {
      const [x, y] = project(p)
      assert.ok(x >= 64.999 && x <= 735.001 && y >= 64.999 && y <= 515.001)
    }
  }
})

test('empty/identical points and poles produce finite centered coordinates', () => {
  assert.deepEqual(createGeoProjector([])(point(0, 0)), [400, 290])
  for (const p of [point(113, 23), point(0, 90), point(0, -90)]) {
    assert.deepEqual(createGeoProjector([p, p])(p), [400, 290])
  }
  assert.equal(validGeoPoint(point(NaN, 23)), false)
  assert.equal(validGeoPoint(point(181, 23)), false)
  assert.deepEqual(createGeoProjector([])(point(NaN, 0)), [400, 290])
})

test('date-line route stays short and ordered', () => {
  const a = point(179.9, 1), b = point(-179.9, 1), mid = point(180, 1)
  const project = createGeoProjector([a, b, mid])
  assert.ok(project(a)[0] < project(mid)[0] && project(mid)[0] < project(b)[0])
})

const event = (name, key, extra = {}) => ({ event: name, candidate_key: ['113.3,23.1', '', key], ...extra })
test('preserves different exit constraints at the same coordinates', () => {
  const dots = projectionCandidates([event('candidate_generated', 'exit-1'), event('candidate_generated', 'exit-2')], [])
  assert.equal(dots.length, 2)
  assert.notEqual(dots[0].key, dots[1].key)
})

test('only point-level failures count as rejection; mode filters and budgets do not', () => {
  const events = [event('candidate_generated', 'a'), event('candidate_accepted', 'a', { recommendation: { id: 'kept' } }),
    { event: 'taxi_filtered', candidate_id: 'kept', reason: 'fare_limit_or_unknown' },
    event('candidate_generated', 'b'), event('candidate_not_evaluated', 'b', { reason: 'early_stop_or_planning_budget' }),
    event('candidate_generated', 'c'), event('candidate_rejected', 'c', { reason: 'detour_limit' })]
  const dots = projectionCandidates(events, [{ id: 'kept' }])
  assert.deepEqual(dots.map(d => d.status), ['recommended', 'pending', 'rejected'])
  assert.equal(dots[2].reason, '司机增加时间超出上限')
})

test('uses sequence order, ignores malformed coordinates and keeps unknown reasons honest', () => {
  const events = [event('candidate_rejected', 'a', { sequence: 3, reason: 'new_future_code' }),
    event('candidate_generated', 'a', { sequence: 1 }), null,
    { event: 'candidate_rejected', candidate_key: ['NaN,23', '', 'x'] },
    { event: 'candidate_rejected', candidate_id: 'unknown' }]
  const dots = projectionCandidates(events, [])
  assert.equal(dots.length, 1)
  assert.equal(dots[0].status, 'rejected')
  assert.equal(dots[0].reason, '未通过本次路线核验')
})
