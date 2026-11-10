<script setup lang="ts">
import { computed } from 'vue'
import { CarFront } from 'lucide-vue-next'
import { coordinates, distance, duration, detourDuration, type Place, type Route, type Recommendation } from '../lib/api'

const props = defineProps<{
  origin: Place | null
  destination: Place | null
  baseline: Route
  recommendation: Recommendation
  demo: boolean
}>()
const remaining = computed(() => Math.max(0, props.recommendation.driver_route.duration_s - props.recommendation.shared_ride_s))
const distanceDelta = computed(() => props.recommendation.driver_route.distance_m - props.baseline.distance_m)
const routes = computed(() => [
  { label: '经下车点的司机路线', route: props.recommendation.driver_route },
  { label: '司机原路线', route: props.baseline },
])
</script>

<template>
  <section class="driver-itinerary detail-panel" aria-label="司机行程方案">
    <h3><CarFront :size="19" />司机行程方案</h3>
    <p v-if="demo" class="driver-muted">演示行程与道路均为模拟数据，不可用于导航。</p>
    <ol class="driver-stops">
      <li><span>A · 出发</span><strong>{{ origin?.name ?? '共同出发地' }}</strong></li>
      <li v-if="recommendation.highway_exit"><span>途经出口</span><strong>{{ recommendation.highway_exit.name }}</strong></li>
      <li><span>P · 乘客下车</span><div><strong>{{ recommendation.point.name }}</strong><small class="driver-point">{{ recommendation.dropoff_road ? `所在道路：${recommendation.dropoff_road} · ` : '' }}{{ recommendation.candidate_source === 'roadside' ? '道路采样点' : '场所周边候选' }}<br />GCJ-02：{{ coordinates(recommendation.point.point) }}</small></div></li>
      <li><span>B · 司机终点</span><strong>{{ destination?.name ?? '司机目的地' }}</strong></li>
    </ol>
    <div class="driver-comparison">
      <div><span>原路线</span><strong>{{ distance(baseline.distance_m) }}</strong><span>驾车 {{ duration(baseline.duration_s) }}</span></div>
      <div><span>经下车点</span><strong>{{ distance(recommendation.driver_route.distance_m) }}</strong><span>司机总时间 {{ duration(recommendation.driver_route.duration_s + recommendation.stop_duration_s) }}（含停靠）</span></div>
      <div><span>变化</span><strong>{{ distanceDelta < 0 ? '减少' : '增加' }} {{ distance(Math.abs(distanceDelta)) }}</strong><span>司机增加约 {{ detourDuration(recommendation.driver_detour_s) }}（含停靠）</span></div>
    </div>
    <p class="driver-timing">到下车点驾车 {{ duration(recommendation.shared_ride_s) }} → 停靠 {{ duration(recommendation.stop_duration_s) }} → 继续驾车 {{ duration(remaining) }} 到司机终点</p>
    <details v-for="item in routes" :key="item.label" class="driver-road-details">
      <summary>{{ item.label }} · 查看行驶路段{{ item.route.driving_steps?.length ? `（${item.route.driving_steps.length}段）` : '' }}</summary>
      <ol v-if="item.route.driving_steps?.length" class="driver-road-steps">
        <li v-for="(step, index) in item.route.driving_steps" :key="index">
          <strong>{{ step.road_name || '未命名道路' }}</strong>
          <p v-if="step.instruction">{{ step.instruction }}</p>
          <p v-if="step.action || step.assistant_action">{{ [step.action, step.assistant_action].filter(Boolean).join(' · ') }}</p>
          <span class="driver-muted">{{ step.distance_m == null ? '路段距离未提供' : distance(step.distance_m) }} · {{ step.duration_s == null ? '路段耗时未提供' : duration(step.duration_s) }}</span>
        </li>
      </ol>
      <p v-else class="driver-muted">当前结果未提供行驶路段，请重新计算获取；路线总览仍可在地图查看。</p>
    </details>
    <p class="driver-muted">预计驾车时间不含停靠；总耗时已另加停靠时间。路线与现场停靠条件请在出发前确认。</p>
  </section>
</template>

<style scoped>
.driver-itinerary h3 { display: flex; align-items: center; gap: 8px; margin: 0 0 15px; color: #2865a8; font-size: 17px; }
.driver-stops { padding: 0; margin: 0 0 16px; list-style: none; display: grid; gap: 10px; }
.driver-stops li { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 10px; font-size: 12px; line-height: 1.7; }
.driver-stops span { color: #7a8b98; }
.driver-stops strong { overflow-wrap: anywhere; }
.driver-point { display: block; color: #7a8b98; font-size: 11px; overflow-wrap: anywhere; }
.driver-comparison { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; padding: 14px; background: #f2f7fc; border-radius: 10px; }
.driver-comparison div { display: flex; flex-direction: column; gap: 7px; font-size: 12px; line-height: 1.6; }
.driver-comparison strong { color: #2865a8; font-size: 15px; }
.driver-timing { font-size: 12px; color: #4b6a85; line-height: 1.9; }
.driver-road-details { border-top: 1px solid #e6edf4; padding: 12px 0; }
.driver-road-details summary { cursor: pointer; font-size: 12px; color: #376c9c; line-height: 1.8; }
.driver-road-steps { padding-left: 24px; font-size: 12px; line-height: 1.8; }
.driver-road-steps li { padding: 8px 0; overflow-wrap: anywhere; }
.driver-road-steps p { margin: 3px 0; }
.driver-muted { color: #7a8b98; font-size: 11px; line-height: 1.8; }
@media (max-width: 640px) {
  .driver-comparison { grid-template-columns: 1fr; }
  .driver-stops li { grid-template-columns: 88px minmax(0, 1fr); }
}
</style>
