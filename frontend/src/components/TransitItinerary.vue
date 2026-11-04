<script setup lang="ts">
import { BusFront, Footprints, TrainFront } from 'lucide-vue-next'
import { duration, type ModeOption } from '../lib/api'

defineProps<{ option: ModeOption }>()
</script>

<template>
  <section class="transit-itinerary" aria-label="公共交通乘坐方式">
    <h4>后续公共交通乘坐方式</h4>
    <ol v-if="option.transit_steps?.length" class="itinerary-steps">
      <li v-for="(step, index) in option.transit_steps" :key="index" class="itinerary-step" :class="step.kind">
        <div class="step-marker"><Footprints v-if="step.kind === 'walk'" :size="17" /><TrainFront v-else-if="step.kind === 'subway'" :size="17" /><BusFront v-else :size="17" /></div>
        <div class="step-content">
          <div class="step-heading"><span>{{ index + 1 }}. {{ step.title }}<template v-if="step.kind !== 'walk'"> · {{ step.line_name || '线路待确认' }}</template></span>
            <span v-if="step.duration_s != null" class="step-duration">约{{ duration(step.duration_s) }}</span>
          </div>
          <template v-if="step.kind === 'walk'">
            <p>{{ step.from_stop || '当前位置' }} → {{ step.to_stop || '目的地' }}<strong v-if="step.distance_m != null"> · {{ step.distance_m }}米</strong><span v-else> · 步行距离待确认</span></p>
            <details v-if="step.instructions?.length"><summary>查看步行指引</summary><ol class="walking-instructions"><li v-for="(instruction, i) in step.instructions" :key="i">{{ instruction }}</li></ol></details>
          </template>
          <template v-else>
            <p class="ride-direction">{{ step.direction ? `开往${step.direction}方向` : '乘车方向信息未提供，请核对站牌' }}</p>
            <p><span class="stop-label">上车</span>{{ step.from_stop || '上车站待确认' }}<span v-if="step.entrance" class="entrance-label"> · {{ step.entrance }}进站</span></p>
            <p><span class="stop-label">下车</span>{{ step.to_stop || '下车站待确认' }}<strong v-if="step.stop_count != null"> · 乘坐{{ step.stop_count }}站</strong><span v-else> · 站数未提供</span></p>
            <p v-if="step.exit" class="exit-label">出站口：{{ step.exit }}</p>
            <details v-if="step.via_stops?.length"><summary>查看途经站点</summary><p class="via-stops">{{ step.via_stops.join(' → ') }}<template v-if="step.to_stop"> → {{ step.to_stop }}（下车）</template></p></details>
          </template>
        </div>
      </li>
    </ol>
    <p v-else class="itinerary-missing">这份结果未包含分段乘坐信息，请重新计算。</p>
  </section>
</template>

<style scoped>
.transit-itinerary { margin: 18px 0; padding: 18px; border: 1px solid #dcebe7; border-radius: 14px; background: #fbfdfc; }
h4 { margin: 0 0 16px; color: #173b34; font-size: 14px; }
.itinerary-steps { list-style: none; padding: 0; margin: 0; }
.itinerary-step { display: grid; grid-template-columns: 28px minmax(0, 1fr); gap: 10px; position: relative; padding-bottom: 20px; }
.itinerary-step:last-child { padding-bottom: 0; }
.itinerary-step:not(:last-child)::before { content: ''; position: absolute; top: 28px; bottom: 3px; left: 13px; border-left: 1px solid #d9e7e1; }
.step-marker { width: 28px; height: 28px; border-radius: 50%; background: #e6f4ef; color: #0c8c70; display: flex; align-items: center; justify-content: center; }
.walk .step-marker { color: #74847d; background: #eef2f0; }
.step-content { min-width: 0; overflow-wrap: anywhere; }
.step-heading { display: flex; gap: 6px 12px; flex-wrap: wrap; align-items: baseline; color: #203c34; font-size: 13px; font-weight: 650; line-height: 1.6; }
.step-duration { color: #7b8b84; font-size: 12px; font-weight: 400; }
p { margin: 6px 0 0; color: #61746b; font-size: 12px; line-height: 1.8; }
strong { color: #18846d; font-weight: 600; }
.ride-direction { color: #16826c; }
.stop-label { display: inline-block; margin-right: 9px; color: #8a9892; }
.entrance-label, .exit-label { color: #4f7164; }
details { margin-top: 7px; font-size: 12px; color: #61746b; }
summary { cursor: pointer; color: #14816c; line-height: 1.8; }
.walking-instructions { padding-left: 20px; margin: 7px 0 0; line-height: 1.9; }
.via-stops { color: #78887f; }
@media (max-width: 600px) { .transit-itinerary { padding: 13px; } .itinerary-step { gap: 8px; } }
</style>
