#!/usr/bin/env python3
r"""AlongRoute 前端输入体验升级脚本 (PlaceField.vue + App.vue + smart-planner.css).

涵盖的 3 项核心 UI/UX 升级:
    [1] 最近常用地点胶囊记忆 (localStorage) — PlaceField.vue:
        - 自动将用户每次选中的有效地点持久化到 `localStorage` (`alongroute_recent_places_v1`，最多保留 6 个去重常用地点)；
        - 当用户聚焦输入框且尚未输入关键字时，自动弹出精美的「最近常用」下拉列表，并支持在输入框底部展示最多 3 个快捷历史胶囊（一键即填，支持一键清空与单框 X 清除按钮）；
        - 修复 `watch(() => props.modelValue)` 在外部清空或互换为 `null` 时未同步清空输入框文字 `textValue` 的问题。
    [2] 共同出发地内嵌「一键定位」徽章 — PlaceField.vue & App.vue:
        - 在「共同出发地 (`kind === 'origin'`)」输入框右侧内嵌紧凑的 `[📍 定位]` 胶囊按钮，点击直接触发定位，无需再去表单最底部找小字按钮；
        - 同时保留并美化底部的快捷操作栏。
    [3] 司机目的地 ⇄ 乘客目的地「一键互换」按钮 — App.vue & smart-planner.css:
        - 新增 `swapDestinations()` 函数与带旋转微交互的 `[⇅ 互换司机/乘客终点]` 胶囊按钮（同时在用户页 `#trip` 插槽与调试页 `.place-list` 中生效），方便测试或互换主客视角。

用法:
    1. 预览修改 Diff (不修改磁盘文件):
       python3 upgrade_place_field_and_app_ui.py --dry-run

    2. 在 AlongRoute 仓库根目录下应用修改并自动创建 .bak 备份:
       python3 upgrade_place_field_and_app_ui.py --backup
"""

from __future__ import annotations

import argparse
import difflib
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PatchResult:
    file_rel: str
    applied: list[str]
    skipped: list[str]
    changed: bool


# ============================================================================
# 1. frontend/src/components/PlaceField.vue 完整升级实现
# ============================================================================

PLACE_FIELD_VUE_NEW = """<script setup lang="ts">
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
"""


def patch_place_field(content: str) -> tuple[str, list[str], list[str]]:
    if "RECENT_STORAGE_KEY" in content:
        return content, [], ["PlaceField.vue 已包含最近常用地点胶囊与内嵌定位按钮"]
    return (
        PLACE_FIELD_VUE_NEW,
        [
            "升级 PlaceField.vue：新增 localStorage 最近常用地点记忆胶囊与下拉面板 (最多6条)",
            "升级 PlaceField.vue：在共同出发地标题栏内嵌『当前位置』一键定位胶囊按钮，并支持单框一键清空 (X)",
            "修复 PlaceField.vue：外部清空或互换 modelValue 时同步更新输入框文本",
        ],
        [],
    )


# ============================================================================
# 2. frontend/src/App.vue: 接入 swapDestinations 与内嵌定位事件
# ============================================================================

def patch_app_vue(content: str) -> tuple[str, list[str], list[str]]:
    applied, skipped = [], []

    # 2.1 Import ArrowUpDown from lucide-vue-next
    if "ArrowUpDown" not in content:
        lucide_pat = re.compile(r"import\s*\{([^}]+)\}\s*from\s*'lucide-vue-next'")
        if lucide_pat.search(content):
            def _repl_lucide(m: re.Match) -> str:
                names = [n.strip() for n in m.group(1).split(",") if n.strip()]
                if "ArrowUpDown" not in names:
                    names.append("ArrowUpDown")
                return f"import {{ {', '.join(names)} }} from 'lucide-vue-next'"

            content = lucide_pat.sub(_repl_lucide, content, count=1)
            applied.append("在 App.vue 中从 lucide-vue-next 导入 ArrowUpDown 互换图标")

    # 2.2 Add swapDestinations() right after useCurrentLocation()
    if "function swapDestinations()" not in content:
        loc_fn_pat = re.compile(
            r"(function useCurrentLocation\(\)\s*\{[\s\S]*?\n\})"
        )
        swap_fn_code = (
            r"\1\n\n"
            "function swapDestinations() {\n"
            "  const prevDriver = driverDestination.value\n"
            "  driverDestination.value = passengerDestination.value\n"
            "  passengerDestination.value = prevDriver\n"
            "}"
        )
        if loc_fn_pat.search(content):
            content = loc_fn_pat.sub(swap_fn_code, content, count=1)
            applied.append("在 App.vue 中新增 swapDestinations() 司机/乘客目的地一键互换函数")
        else:
            skipped.append("未匹配到 useCurrentLocation() 函数末尾")
    else:
        skipped.append("App.vue 已包含 swapDestinations()")

    # 2.3 Upgrade #trip slot in SmartPlanner with @locate, :locating, and the swap destinations button
    trip_slot_pat = re.compile(
        r"(?P<ind>[ \t]*)<template #trip>\s*\n"
        r'(?P<ind2>[ \t]*)<PlaceField v-model="origin" kind="origin" label="共同出发地" placeholder="[^"]*" />\s*\n'
        r'(?P=ind2)<PlaceField v-model="driverDestination" kind="driver" label="司机目的地" placeholder="[^"]*" />\s*\n'
        r'(?P=ind2)<PlaceField v-model="passengerDestination" kind="passenger" label="乘客目的地" placeholder="[^"]*" />\s*\n'
        r'(?P=ind2)<button class="current-location" :disabled="locating" @click="useCurrentLocation">[\s\S]*?</button>\s*\n'
        r"(?P=ind)</template>"
    )
    if "swapDestinations" in content and "swap-destinations-btn" not in content and trip_slot_pat.search(content):
        def _repl_trip(m: re.Match) -> str:
            ind = m.group("ind")
            ind2 = m.group("ind2")
            return (
                f"{ind}<template #trip>\n"
                f'{ind2}<PlaceField v-model="origin" kind="origin" label="共同出发地" placeholder="搜索司机与乘客的出发位置" :locating="locating" @locate="useCurrentLocation" />\n'
                f'{ind2}<PlaceField v-model="driverDestination" kind="driver" label="司机目的地" placeholder="搜索司机目的地" />\n'
                f'{ind2}<PlaceField v-model="passengerDestination" kind="passenger" label="乘客目的地" placeholder="搜索乘客目的地" />\n'
                f'{ind2}<div class="trip-quick-actions">\n'
                f'{ind2}  <button type="button" class="swap-destinations-btn" :disabled="!driverDestination && !passengerDestination" @click="swapDestinations">\n'
                f'{ind2}    <ArrowUpDown :size="14" />互换司机/乘客终点\n'
                f"{ind2}  </button>\n"
                f'{ind2}  <button type="button" class="current-location" :disabled="locating" @click="useCurrentLocation">\n'
                f'{ind2}    <MapPin :size="14" />{{{{ locating ? \'定位中...\' : \'使用当前位置作为出发地\' }}}}\n'
                f"{ind2}  </button>\n"
                f"{ind2}</div>\n"
                f"{ind}</template>"
            )

        content = trip_slot_pat.sub(_repl_trip, content, count=1)
        applied.append("升级 App.vue #trip 插槽：绑定内嵌定位事件并新增『互换司机/乘客终点』快捷按钮栏")
    elif "swap-destinations-btn" in content:
        skipped.append("App.vue #trip 插槽已包含互换终点按钮")
    else:
        skipped.append("未匹配到 App.vue 中的 <template #trip> 块")

    return content, applied, skipped


# ============================================================================
# 3. frontend/src/smart-planner.css: 新增历史胶囊、内嵌定位与互换按钮样式
# ============================================================================

SMART_PLANNER_CSS_ADDITIONS = """
/* === PlaceField Recent Chips, Inline Locate & Destination Swap Upgrades === */
.smart-planner .place-label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.smart-planner .place-locate-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid #bfdbfe;
  background: #eff6ff;
  color: #1d4ed8;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s ease;
}

.smart-planner .place-locate-pill:hover:not(:disabled) {
  background: #dbeafe;
  border-color: #93c5fd;
}

.smart-planner .place-locate-pill:disabled {
  opacity: 0.6;
  cursor: wait;
}

.smart-planner .place-input-row {
  display: flex;
  align-items: center;
  gap: 6px;
}

.smart-planner .place-input-row input {
  flex: 1;
  min-width: 0;
}

.smart-planner .place-clear-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 999px;
  border: 0;
  background: #f1f5f9;
  color: #64748b;
  cursor: pointer;
  padding: 0;
  flex-shrink: 0;
  transition: background 0.15s ease, color 0.15s ease;
}

.smart-planner .place-clear-btn:hover {
  background: #e2e8f0;
  color: #0f172a;
}

.smart-planner .place-quick-chips {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 6px;
}

.smart-planner .quick-chip-label {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 11px;
  color: #64748b;
  font-weight: 500;
}

.smart-planner .place-quick-chip {
  display: inline-flex;
  align-items: center;
  max-width: 128px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid #e2e8f0;
  background: #f8fafc;
  color: #334155;
  font-size: 11px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s ease;
}

.smart-planner .place-quick-chip:hover {
  border-color: #94a3b8;
  background: #f1f5f9;
  color: #0f172a;
}

.smart-planner .recent-suggestions-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 2px 6px 8px;
  margin-bottom: 4px;
  border-bottom: 1px solid #f1f5f9;
  font-size: 11px;
  font-weight: 600;
  color: #64748b;
}

.smart-planner .recent-suggestions-head span {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.smart-planner .clear-recent-btn {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  border: 0;
  background: none;
  color: #94a3b8;
  font-size: 11px;
  cursor: pointer;
  padding: 2px 4px;
  border-radius: 4px;
}

.smart-planner .clear-recent-btn:hover {
  color: #dc2626;
  background: #fef2f2;
}

.smart-planner .trip-quick-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  padding-top: 2px;
}

.smart-planner .swap-destinations-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 11px;
  border-radius: 999px;
  border: 1px solid #cbd5e1;
  background: #ffffff;
  color: #334155;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s ease;
}

.smart-planner .swap-destinations-btn:hover:not(:disabled) {
  border-color: #059669;
  color: #047857;
  background: #ecfdf5;
}

.smart-planner .swap-destinations-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
"""


def patch_smart_planner_css(content: str) -> tuple[str, list[str], list[str]]:
    if "place-quick-chips" in content:
        return content, [], ["smart-planner.css 已包含历史胶囊与互换按钮样式"]
    return (
        content.rstrip() + "\n" + SMART_PLANNER_CSS_ADDITIONS,
        ["在 smart-planner.css 中新增常用地点胶囊、最近常用下拉头、内嵌定位胶囊及终点互换按钮样式"],
        [],
    )


# ============================================================================
# 主执行器
# ============================================================================

PATCH_PIPELINE = [
    ("frontend/src/components/PlaceField.vue", patch_place_field),
    ("frontend/src/App.vue", patch_app_vue),
    ("frontend/src/smart-planner.css", patch_smart_planner_css),
]


def apply_all(repo_dir: Path, dry_run: bool = False, backup: bool = False) -> list[PatchResult]:
    results: list[PatchResult] = []
    for rel_path, patcher in PATCH_PIPELINE:
        fpath = repo_dir / rel_path
        if not fpath.exists():
            results.append(PatchResult(rel_path, [], [f"文件不存在: {fpath}"], False))
            continue

        original = fpath.read_text(encoding="utf-8")
        updated, applied, skipped = patcher(original)
        changed = updated != original

        if changed:
            if dry_run:
                diff = difflib.unified_diff(
                    original.splitlines(keepends=True),
                    updated.splitlines(keepends=True),
                    fromfile=f"a/{rel_path}",
                    tofile=f"b/{rel_path}",
                )
                sys.stdout.writelines(diff)
            else:
                if backup:
                    shutil.copy2(fpath, fpath.with_suffix(fpath.suffix + ".bak"))
                fpath.write_text(updated, encoding="utf-8")

        results.append(PatchResult(rel_path, applied, skipped, changed))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AlongRoute 前端输入体验升级脚本 (常用地点记忆胶囊 + 内嵌定位 + 起终点互换)"
    )
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path.cwd(),
        help="AlongRoute 仓库根目录路径 (默认: 当前目录)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印 Unified Diff 预览，不修改磁盘文件",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="修改前为每个受影响的文件创建 .bak 备份",
    )
    args = parser.parse_args()

    repo_dir = args.repo_dir.resolve()
    if not (repo_dir / "frontend" / "src").exists():
        print(
            f"[提示] 在 {repo_dir} 下未检测到 frontend/src 目录，请通过 --repo-dir 指定 AlongRoute 仓库根目录。",
            file=sys.stderr,
        )
        return 1

    results = apply_all(repo_dir, dry_run=args.dry_run, backup=args.backup)

    mode_label = "DRY-RUN 预览" if args.dry_run else "已写入磁盘"
    print(f"\n=== AlongRoute 前端输入体验升级摘要 ({mode_label}) ===")
    for res in results:
        status = "UPDATED" if res.changed else "UNCHANGED"
        print(f"\n[{status}] {res.file_rel}")
        for item in res.applied:
            print(f"  + {item}")
        for item in res.skipped:
            print(f"  - (跳过) {item}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())