<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { Clock, LocateFixed, MapPin, Navigation, Search, Trash2, UserRound, X } from 'lucide-vue-next'
import { apiGet, type Place, type PlaceSuggestion } from '../lib/api'

const RECENT_STORAGE_KEY = 'alongroute_recent_places_v1'
const MAX_RECENT_PLACES = 6

const props = defineProps<{
  modelValue: Place | null
  label: string
  kind: 'origin' | 'driver' | 'passenger'
  placeholder: string
  locating?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: Place | null]
  'locate': []
}>()

const inputId = useId()
const textValue = ref(props.modelValue?.name || '')
const focused = ref(false)
const suggestions = ref<PlaceSuggestion[]>([])
const recentPlaces = ref<PlaceSuggestion[]>([])
const loading = ref(false)
const searchError = ref('')
let blurTimer: ReturnType<typeof setTimeout> | undefined
let timer: ReturnType<typeof setTimeout> | undefined
let sequence = 0

function loadRecentPlaces(): PlaceSuggestion[] {
  try {
    const raw = localStorage.getItem(RECENT_STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (item): item is PlaceSuggestion =>
        Boolean(item && typeof item.name === 'string' && item.point && typeof item.point.lon === 'number' && typeof item.point.lat === 'number')
    ).slice(0, MAX_RECENT_PLACES)
  } catch {
    return []
  }
}

function saveRecentPlace(item: PlaceSuggestion) {
  try {
    const current = loadRecentPlaces()
    const keyOf = (p: PlaceSuggestion) => `${p.poi_id || p.name}:${p.point.lon.toFixed(5)},${p.point.lat.toFixed(5)}`
    const targetKey = keyOf(item)
    const next = [item, ...current.filter(p => keyOf(p) !== targetKey)].slice(0, MAX_RECENT_PLACES)
    localStorage.setItem(RECENT_STORAGE_KEY, JSON.stringify(next))
    recentPlaces.value = next
  } catch {
    /* ignore storage quota errors */
  }
}

function clearRecentPlaces() {
  try {
    localStorage.removeItem(RECENT_STORAGE_KEY)
  } catch {
    /* ignore */
  }
  recentPlaces.value = []
}

onMounted(() => {
  recentPlaces.value = loadRecentPlaces()
})

watch(() => props.modelValue, next => {
  textValue.value = next ? next.name : ''
  if (next) searchError.value = ''
})

const showRecentDropdown = computed(() =>
  focused.value && !textValue.value.trim() && !suggestions.value.length && recentPlaces.value.length > 0
)

const quickChips = computed(() => recentPlaces.value.slice(0, 3))

function onFocus() {
  clearTimeout(blurTimer)
  recentPlaces.value = loadRecentPlaces()
  focused.value = true
}

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
        `/api/places/suggest?q=${encodeURIComponent(textValue.value.trim())}`
      )
      if (sequence === thisSequence) {
        suggestions.value = response.items
        if (!response.items.length) searchError.value = '未找到地点，请补充城市或详细地址'
      }
    } catch {
      if (sequence === thisSequence) searchError.value = '搜索失败，请重试'
    } finally {
      if (sequence === thisSequence) loading.value = false
    }
  }, 220)
}

function selectSuggestion(item: PlaceSuggestion) {
  sequence++
  clearTimeout(timer)
  clearTimeout(blurTimer)
  loading.value = false
  searchError.value = ''
  textValue.value = item.name
  saveRecentPlace(item)
  emit('update:modelValue', {
    name: item.name,
    point: item.point,
    citycode: item.citycode,
    poi_id: item.poi_id,
  })
  focused.value = false
  suggestions.value = []
}

function clearField() {
  sequence++
  clearTimeout(timer)
  loading.value = false
  searchError.value = ''
  textValue.value = ''
  suggestions.value = []
  emit('update:modelValue', null)
}

function loseFocus() {
  // Delay allows pointer click on suggestion to select before popup is hidden.
  blurTimer = setTimeout(() => { focused.value = false }, 160)
}

onBeforeUnmount(() => { sequence++; clearTimeout(timer); clearTimeout(blurTimer) })
</script>

<template>
  <div class="place-field" :class="[kind, { 'has-value': Boolean(modelValue) }]">
    <div class="place-icon">
      <MapPin v-if="kind === 'origin'" :size="17" />
      <Navigation v-else-if="kind === 'driver'" :size="17" />
      <UserRound v-else :size="17" />
    </div>
    <div class="place-input-wrap">
      <div class="place-label-row">
        <label :for="inputId">{{ label }}</label>
        <button
          v-if="kind === 'origin'"
          type="button"
          class="place-locate-pill"
          :disabled="locating"
          title="使用当前定位作为共同出发地"
          @click.stop="emit('locate')"
        >
          <LocateFixed :size="12" />
          <span>{{ locating ? '定位中...' : '当前位置' }}</span>
        </button>
      </div>
      <div class="place-input-row">
        <input
          :id="inputId"
          :value="textValue"
          :placeholder="placeholder"
          autocomplete="off"
          @input="typeValue"
          @focus="onFocus"
          @blur="loseFocus"
        />
        <button
          v-if="textValue"
          type="button"
          class="place-clear-btn"
          title="清空此地点"
          aria-label="清空此地点"
          @pointerdown.prevent="clearField"
        >
          <X :size="14" />
        </button>
      </div>
      <div v-if="!modelValue && !textValue && quickChips.length" class="place-quick-chips">
        <span class="quick-chip-label"><Clock :size="11" />常用:</span>
        <button
          v-for="chip in quickChips"
          :key="`chip-${chip.poi_id ?? chip.name}-${chip.point.lon}`"
          type="button"
          class="place-quick-chip"
          @pointerdown.prevent="selectSuggestion(chip)"
        >
          {{ chip.name }}
        </button>
      </div>
      <p v-if="!modelValue && textValue" class="unselected">请从搜索建议中选择地点</p>
    </div>
    <p v-if="searchError && focused" class="unselected">{{ searchError }}</p>
    <Search v-if="!textValue" class="place-search-icon" :size="15" />

    <div v-if="focused && suggestions.length" class="place-suggestions">
      <button
        v-for="item in suggestions"
        :key="`${item.poi_id ?? item.name}-${item.point.lon}`"
        type="button"
        class="suggestion"
        @pointerdown.prevent="selectSuggestion(item)"
      >
        <MapPin :size="14" />
        <span><strong>{{ item.name }}</strong><small>{{ item.address }}</small></span>
      </button>
    </div>

    <div v-else-if="showRecentDropdown" class="place-suggestions recent-suggestions">
      <div class="recent-suggestions-head">
        <span><Clock :size="12" /> 最近常用地点</span>
        <button type="button" class="clear-recent-btn" @pointerdown.prevent="clearRecentPlaces">
          <Trash2 :size="12" /> 清空
        </button>
      </div>
      <button
        v-for="item in recentPlaces"
        :key="`recent-${item.poi_id ?? item.name}-${item.point.lon}`"
        type="button"
        class="suggestion"
        @pointerdown.prevent="selectSuggestion(item)"
      >
        <Clock :size="14" />
        <span><strong>{{ item.name }}</strong><small>{{ item.address || '历史选中地点' }}</small></span>
      </button>
    </div>
  </div>
</template>
