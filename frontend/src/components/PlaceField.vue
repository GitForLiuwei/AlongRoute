<script setup lang="ts">
import { ref, watch, onBeforeUnmount, useId } from 'vue'
import { MapPin, Navigation, UserRound, Search } from 'lucide-vue-next'
import { apiGet, type Place, type PlaceSuggestion } from '../lib/api'

const props = defineProps<{
  modelValue: Place | null
  label: string
  kind: 'origin' | 'driver' | 'passenger'
  placeholder: string
}>()
const emit = defineEmits<{ 'update:modelValue': [value: Place | null] }>()
const inputId = useId()
const textValue = ref(props.modelValue?.name || '')
const focused = ref(false)
const suggestions = ref<PlaceSuggestion[]>([])
const loading = ref(false)
const searchError = ref('')
let blurTimer: ReturnType<typeof setTimeout> | undefined
let timer: ReturnType<typeof setTimeout> | undefined
let sequence = 0
watch(() => props.modelValue, next => {
  if (next) textValue.value = next.name
})

function typeValue(e: Event) {
  textValue.value = (e.target as HTMLInputElement).value
  emit('update:modelValue', null)
  searchError.value = ''
  loading.value = false
  sequence += 1
  const thisSequence = sequence
  clearTimeout(timer)
  suggestions.value = []
  if (!textValue.value.trim()) return
  timer = setTimeout(async () => {
    loading.value = true
    try {
      const response = await apiGet<{ items: PlaceSuggestion[] }>(
        `/api/places/suggest?q=${encodeURIComponent(textValue.value.trim())}`)
      if (sequence === thisSequence) { suggestions.value = response.items; if (!response.items.length) searchError.value = '未找到地点，请补充城市或详细地址' }
    } catch {
      if (sequence === thisSequence) { suggestions.value = []; searchError.value = '地点搜索失败，请稍后重新输入' }
    } finally {
      if (sequence === thisSequence) loading.value = false
    }
  }, 280)
}
function selectSuggestion(item: PlaceSuggestion) {
  sequence++
  clearTimeout(timer)
  clearTimeout(blurTimer)
  loading.value = false
  textValue.value = item.name
  emit('update:modelValue', {
    name: item.name, point: item.point, citycode: item.citycode, poi_id: item.poi_id,
  })
  focused.value = false
  suggestions.value = []
}
function loseFocus() {
  // Delay allows pointer click on suggestion to select before popup is hidden.
  blurTimer = setTimeout(() => { focused.value = false }, 160)
}
onBeforeUnmount(() => { sequence++; clearTimeout(timer); clearTimeout(blurTimer) })
</script>

<template>
  <div class="place-field" :class="kind">
    <div class="place-icon">
      <MapPin v-if="kind === 'origin'" :size="17" />
      <Navigation v-else-if="kind === 'driver'" :size="17" />
      <UserRound v-else :size="17" />
    </div>
    <div class="place-input-wrap">
      <label :for="inputId">{{ label }}</label>
      <input :id="inputId" :value="textValue" :placeholder="placeholder" autocomplete="off"
        @input="typeValue" @focus="focused = true" @blur="loseFocus" />
      <p v-if="!modelValue && textValue" class="unselected">请从搜索建议中选择地点</p>
    </div>
    <p v-if="searchError && focused" class="unselected">{{ searchError }}</p>
    <Search class="place-search-icon" :size="15" />
    <div v-if="focused && suggestions.length" class="place-suggestions">
      <button v-for="item in suggestions" :key="`${item.poi_id ?? item.name}-${item.point.lon}`"
        class="suggestion" @pointerdown.prevent="selectSuggestion(item)">
        <MapPin :size="14" />
        <span><strong>{{ item.name }}</strong><small>{{ item.address }}</small></span>
      </button>
    </div>
    <div v-else-if="focused && loading" class="place-suggestions waiting">搜索中…</div>
  </div>
</template>
