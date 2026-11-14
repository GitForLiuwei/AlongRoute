#!/usr/bin/env python3
r"""AlongRoute iOS 端产品化升级一键脚本 (去掉地图面板 + 原生拉起高德地图 + 移除所有开发者调试入口).

本脚本将 iOS App 从「网页套壳带调试台」改造为「纯净、可直接交付给最终用户使用的原生工具级 App」：
    [1] 移除所有开发者调试与内部工程痕迹 (App.vue + SmartPlanner.vue):
        - 在 iOS 原生环境 (`isNativeApp`) 下彻底禁用 `/debug` 调试工作台，顶栏不再渲染「开发调试」链接与「真实路线/演示模式」内部徽章；
        - 隐藏「重播地图寻路动画」按钮，品牌副标题升级为「智能顺路下车点推荐」；
        - 将所有面向开发者的报错文案（如提及 FastAPI、后端端口、本地网络）替换为友好的消费者级提示（「网络连接不太稳定，请稍后重试」）。
    [2] 去掉 iOS 端地图展示部分，升级为全宽卡片式单栏原生体验 (SmartPlanner.vue + native.css):
        - 在 `isNativeApp` 下不挂载 `<section class="smart-map-panel">` 与 `<MapCanvas>`，彻底消除 Web 端高德 JS Key 依赖与地图手势滚动冲突；
        - 将原本挂载在地图面板里的「路线与乘车详情 (`#details`)」内联嵌入到选中的推荐卡片下方，支持在卡片内一键展开「路线详情（含公交/地铁步骤与司机途经步骤）」。
    [3] 一键直接拉起高德地图 App 导航与查看位置 (platform.ts + Info.plist + SmartPlanner.vue + App.vue):
        - 在 `frontend/ios/App/App/Info.plist` 中注册 `LSApplicationQueriesSchemes` (`iosamap`, `amapuri`)；
        - 在 `frontend/src/lib/platform.ts` 中新增 `openAmapNavigation(origin, destination, via)`（使用 `iosamap://path?...` 携带起点 A、途经下车点 P、司机终点 B 直接拉起高德驾车规划）与 `openAmapMarker(place)`（使用 `iosamap://viewMap?...` 查看下车点位置），未安装高德 App 时自动回退至 Web 导航页。

用法:
    1. 预览修改 Diff (不修改磁盘文件):
       python3 upgrade_ios_consumer_app.py --dry-run

    2. 在 AlongRoute 仓库根目录下应用修改并生成 .bak 备份:
       python3 upgrade_ios_consumer_app.py --backup
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
# 1. frontend/ios/App/App/Info.plist: 注册 iosamap / amapuri URL Scheme 白名单
# ============================================================================

INFO_PLIST_SCHEMES = """\t<key>LSApplicationQueriesSchemes</key>
\t<array>
\t\t<string>iosamap</string>
\t\t<string>amapuri</string>
\t</array>
\t<key>LSRequiresIPhoneOS</key>"""


def patch_info_plist(content: str) -> tuple[str, list[str], list[str]]:
    if "LSApplicationQueriesSchemes" in content:
        return content, [], ["Info.plist 已包含 LSApplicationQueriesSchemes (iosamap/amapuri)"]
    if "<key>LSRequiresIPhoneOS</key>" in content:
        content = content.replace("<key>LSRequiresIPhoneOS</key>", INFO_PLIST_SCHEMES, 1)
        return (
            content,
            ["在 Info.plist 中注册 LSApplicationQueriesSchemes (iosamap, amapuri) 以支持原生拉起高德地图 App"],
            [],
        )
    return content, [], ["未在 Info.plist 中找到 <key>LSRequiresIPhoneOS</key> 锚点"]


# ============================================================================
# 2. frontend/src/lib/platform.ts: 新增 iosamap:// 原生拉起高德导航与标点函数
# ============================================================================

PLATFORM_TS_NEW = """import { Capacitor } from '@capacitor/core'
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
"""


def patch_platform_ts(content: str) -> tuple[str, list[str], list[str]]:
    if "openAmapNavigation" in content:
        return content, [], ["platform.ts 已包含 openAmapNavigation / openAmapMarker 原生高德拉起函数"]
    return (
        PLATFORM_TS_NEW,
        [
            "升级 platform.ts：新增 buildAmapNavigationUrl、openAmapNavigation 与 openAmapMarker，支持 iosamap:// 原生拉起高德地图（含途经点 P）及未安装自动回退",
        ],
        [],
    )


# ============================================================================
# 3. frontend/src/App.vue: 禁用 iOS 端调试模式、隐藏开发入口、优化消费者文案与高德拉起
# ============================================================================

def patch_app_vue(content: str) -> tuple[str, list[str], list[str]]:
    applied, skipped = [], []

    # 3.1 Import openAmapMarker, buildAmapNavigationUrl from ./lib/platform
    if "openAmapMarker" not in content:
        plat_import_pat = re.compile(r"import\s*\{([^}]+)\}\s*from\s*'./lib/platform'")
        if plat_import_pat.search(content):
            def _repl_plat(m: re.Match) -> str:
                names = [n.strip() for n in m.group(1).split(",") if n.strip()]
                for extra in ("buildAmapNavigationUrl", "buildAmapMarkerUrl", "openAmapMarker"):
                    if extra not in names:
                        names.append(extra)
                return f"import {{ {', '.join(names)} }} from './lib/platform'"

            content = plat_import_pat.sub(_repl_plat, content, count=1)
            applied.append("在 App.vue 中导入 openAmapMarker 与 buildAmapNavigationUrl")

    # 3.2 Force debugPageFromLocation() to always return false when isNativeApp is true
    old_debug_fn = (
        "function debugPageFromLocation() {\n"
        "  if (window.location.hash === '#/debug') return true\n"
        "  if (window.location.hash === '#/user') return false\n"
        "  return window.location.pathname.replace(/\\/$/, '') === '/debug'\n"
        "}"
    )
    new_debug_fn = (
        "function debugPageFromLocation() {\n"
        "  if (isNativeApp) return false\n"
        "  if (window.location.hash === '#/debug') return true\n"
        "  if (window.location.hash === '#/user') return false\n"
        "  return window.location.pathname.replace(/\\/$/, '') === '/debug'\n"
        "}"
    )
    if "if (isNativeApp) return false" not in content and old_debug_fn in content:
        content = content.replace(old_debug_fn, new_debug_fn, 1)
        applied.append("在 App.vue 中强制锁定 iOS 原生端始终处于用户模式 (禁用 /debug 调试页)")

    # 3.3 Replace viewPlace() in App.vue to use openAmapMarker on native iOS
    old_view_place = (
        "function viewPlace() {\n"
        "  if (!active.value || result.value?.mode !== 'live' || stale.value || expired.value) return\n"
        "  window.open(markerUrl(active.value), '_blank', 'noopener,noreferrer')\n"
        "}"
    )
    new_view_place = (
        "function viewPlace() {\n"
        "  if (!active.value || result.value?.mode !== 'live' || stale.value || expired.value) return\n"
        "  openAmapMarker(active.value.point)\n"
        "}"
    )
    if "openAmapMarker(active.value.point)" not in content and old_view_place in content:
        content = content.replace(old_view_place, new_view_place, 1)
        applied.append("升级 App.vue viewPlace()：在 iOS 端点击『在高德查看候选位置』直接拉起高德地图 App")

    # 3.4 Soften developer-facing backend connection error message on iOS
    old_err_msg = "'无法连接规划服务。请确认后端已启动、服务地址正确，并允许本地网络访问；恢复后重新打开应用。'"
    new_err_msg = "'网络连接不太稳定，暂时无法获取路线规划，请检查网络后重试。'"
    if old_err_msg in content:
        content = content.replace(old_err_msg, new_err_msg, 1)
        applied.append("优化 iOS 端网络异常提示文案为消费者友好表达")

    # 3.5 Hide `<template #navigation><a href="/debug"...>开发调试</a></template>` on iOS native app
    old_nav_slot = '<template #navigation><a href="/debug" @click="navigatePage($event, \'/debug\')">开发调试</a></template>'
    new_nav_slot = '<template #navigation><a v-if="!isNativeApp" href="/debug" @click="navigatePage($event, \'/debug\')">开发调试</a></template>'
    if old_nav_slot in content:
        content = content.replace(old_nav_slot, new_nav_slot, 1)
        applied.append("在 iOS 端顶栏彻底隐藏『开发调试』入口按钮")

    return content, applied, skipped


# ============================================================================
# 4. frontend/src/components/SmartPlanner.vue:
#    - iOS 端去掉地图展示面板 (<section class="smart-map-panel" v-if="!isNativeApp">)
#    - 隐藏重播地图动画按钮与真实/演示模式工程标签
#    - 在推荐卡片内新增「路线详情」折叠抽屉与「打开高德地图导航」原生拉起按钮
# ============================================================================

def patch_smart_planner(content: str) -> tuple[str, list[str], list[str]]:
    applied, skipped = [], []

    # 4.1 Import isNativeApp, openAmapNavigation, buildAmapNavigationUrl in SmartPlanner.vue
    if "openAmapNavigation" not in content:
        if "from '../lib/platform'" in content:
            content = re.sub(
                r"import\s*\{([^}]+)\}\s*from\s*'\.\./lib/platform'",
                r"import { \1, isNativeApp, openAmapNavigation, buildAmapNavigationUrl } from '../lib/platform'",
                content,
                count=1,
            )
        else:
            content = content.replace(
                "import MapCanvas from './MapCanvas.vue'",
                "import MapCanvas from './MapCanvas.vue'\nimport { isNativeApp, openAmapNavigation, buildAmapNavigationUrl } from '../lib/platform'",
                1,
            )
        applied.append("在 SmartPlanner.vue 中导入 isNativeApp 与 openAmapNavigation 原生高德导航拉起工具")

    # 4.2 Add startNavigation(r: Recommendation) helper right after navigationUrl(r: Recommendation)
    old_nav_url = (
        "function navigationUrl(r: Recommendation) {\n"
        "  const point = (p: Place) => `${p.point.lon},${p.point.lat},${encodeURIComponent(p.name)}`\n"
        "  return props.snapshot\n"
        "    ? `https://uri.amap.com/navigation?from=${point(props.snapshot.origin)}&to=${point(props.snapshot.driver_destination)}&via=${point(r.point)}&mode=car&policy=0&callnative=1`\n"
        "    : `https://uri.amap.com/marker?position=${r.point.point.lon},${r.point.point.lat}&name=${encodeURIComponent(r.point.name)}`\n"
        "}"
    )
    new_nav_url = (
        "function navigationUrl(r: Recommendation) {\n"
        "  return buildAmapNavigationUrl(r.point, props.snapshot?.origin, props.snapshot?.driver_destination, false)\n"
        "}\n"
        "function startNavigation(r: Recommendation) {\n"
        "  openAmapNavigation(r.point, props.snapshot?.origin ?? props.origin, props.snapshot?.driver_destination ?? props.driverDestination)\n"
        "}"
    )
    if "function startNavigation(" not in content and old_nav_url in content:
        content = content.replace(old_nav_url, new_nav_url, 1)
        applied.append("在 SmartPlanner.vue 中新增 startNavigation()：支持一键携带起点、途经下车点、终点拉起高德地图 App")

    # 4.3 Clean up header subtitle and hide mode badge & replay animation button on iOS
    old_header = (
        '      <a class="smart-brand" href="/user" aria-label="顺路达首页">\n'
        '        <span class="smart-logo"><Compass :size="29" :stroke-width="2.5" /></span>\n'
        '        <span><strong>顺路达 <b>AlongRoute</b><em>高德底图 · 实时筛点</em></strong><small>司机少绕一点路 · 乘客轻松到家</small></span>\n'
        "      </a>\n"
        '      <div class="smart-header-end"><span class="smart-mode" :class="{ demo: mode === \'demo\' }"><i />{{ mode === \'live\' ? \'真实路线\' : \'演示模式\' }}</span><slot name="navigation" /></div>'
    )
    new_header = (
        '      <a class="smart-brand" href="#/user" aria-label="顺路达首页" @click.prevent>\n'
        '        <span class="smart-logo"><Compass :size="29" :stroke-width="2.5" /></span>\n'
        '        <span><strong>顺路达 <b>AlongRoute</b><em v-if="!isNativeApp">高德底图 · 实时筛点</em><em v-else>智能顺路下车</em></strong><small>司机少绕一点路 · 乘客轻松到家</small></span>\n'
        "      </a>\n"
        '      <div v-if="!isNativeApp" class="smart-header-end"><span class="smart-mode" :class="{ demo: mode === \'demo\' }"><i />{{ mode === \'live\' ? \'真实路线\' : \'演示模式\' }}</span><slot name="navigation" /></div>'
    )
    if old_header in content:
        content = content.replace(old_header, new_header, 1)
        applied.append("优化 SmartPlanner 顶栏：在 iOS 端移除『高德底图』、『真实路线/演示模式』及调试导航区")

    old_replay_btn = '<button class="smart-text-button" :disabled="!result || loading || stale || expired" @click="replay"><RefreshCcw :size="16" />重播地图寻路动画</button>'
    new_replay_btn = '<button v-if="!isNativeApp" class="smart-text-button" :disabled="!result || loading || stale || expired" @click="replay"><RefreshCcw :size="16" />重播地图寻路动画</button>'
    if old_replay_btn in content:
        content = content.replace(old_replay_btn, new_replay_btn, 1)
        applied.append("在 iOS 端隐藏『重播地图寻路动画』按钮")

    # 4.4 Upgrade card actions to directly invoke startNavigation(r) (pulling up AMap App) and inline route details toggle on iOS
    old_card_actions = (
        '                <div v-if="active && key(active) === key(r)" class="smart-card-actions" @click.stop>\n'
        '                  <button :disabled="!usable" @click="openShare"><Share2 :size="19" />发给司机确认此点</button>\n'
        '                  <a v-if="usable" :href="navigationUrl(r)" target="_blank" rel="noopener noreferrer"><Navigation :size="19" />导航</a>\n'
        '                  <button v-else disabled class="smart-card-navigation"><Navigation :size="19" />导航</button>\n'
        "                </div>"
    )
    new_card_actions = (
        '                <div v-if="active && key(active) === key(r)" class="smart-card-actions" @click.stop>\n'
        '                  <button type="button" class="smart-nav-primary" :disabled="!usable" @click="startNavigation(r)"><Navigation :size="18" />高德地图导航</button>\n'
        '                  <button type="button" :disabled="!usable" @click="openShare"><Share2 :size="18" />发给司机确认</button>\n'
        '                  <button v-if="isNativeApp" type="button" class="smart-inline-detail-btn" :aria-expanded="detailsOpen" @click="detailsOpen = !detailsOpen">{{ detailsOpen ? \'收起路线详情\' : \'查看路线详情\' }}</button>\n'
        "                </div>\n"
        '                <div v-if="isNativeApp && detailsOpen && active && key(active) === key(r)" class="smart-inline-details" @click.stop>\n'
        '                  <slot name="details" />\n'
        "                </div>"
    )
    if old_card_actions in content:
        content = content.replace(old_card_actions, new_card_actions, 1)
        applied.append("升级推荐卡片操作栏：新增『高德地图导航』一键原生拉起按钮，并在 iOS 卡片内直接内联展开乘车/驾车步骤详情")

    # 4.5 Remove the entire map panel on iOS (`v-if="!isNativeApp"`)
    old_map_panel = '<section class="smart-map-panel" aria-label="路线可视化">'
    new_map_panel = '<section v-if="!isNativeApp" class="smart-map-panel" aria-label="路线可视化">'
    if old_map_panel in content:
        content = content.replace(old_map_panel, new_map_panel, 1)
        applied.append("在 iOS 端移除地图展示面板 (<section v-if=\"!isNativeApp\" class=\"smart-map-panel\">)，不再加载高德 Web 底图")

    return content, applied, skipped


# ============================================================================
# 5. frontend/src/native.css: 打造纯净 iOS 消费者级 App 单栏卡片式视觉体验
# ============================================================================

NATIVE_CSS_NEW = """/* WKWebView consumer iOS app styling: map-free single-column layout, safe-area aware, smooth native feel. */
.native-ios body {
  padding-left: env(safe-area-inset-left, 0px);
  padding-right: env(safe-area-inset-right, 0px);
  padding-bottom: env(safe-area-inset-bottom, 0px);
  background: #f4f7f9;
  -webkit-tap-highlight-color: transparent;
  user-select: none;
}

.native-ios input,
.native-ios select,
.native-ios textarea,
.native-ios pre {
  font-size: 16px;
  user-select: text;
}

.native-ios .smart-header {
  position: sticky;
  top: 0;
  z-index: 30;
  padding-top: calc(10px + env(safe-area-inset-top, 0px));
  padding-bottom: 12px;
  background: rgba(255, 255, 255, 0.92);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border-bottom: 1px solid #e2e8f0;
}

.native-ios .smart-layout {
  display: block;
  max-width: 640px;
  margin: 0 auto;
  min-height: calc(100dvh - 70px);
}

.native-ios .smart-sidebar {
  width: 100%;
  max-width: 100%;
  border-right: 0;
  background: transparent;
  padding-bottom: calc(28px + env(safe-area-inset-bottom, 0px));
}

.native-ios .smart-input-section,
.native-ios .smart-results {
  margin: 12px 14px;
  padding: 18px 16px;
  background: #ffffff;
  border-radius: 20px;
  border: 1px solid #e2e8f0;
  box-shadow: 0 6px 20px rgba(15, 23, 42, 0.04);
}

.native-ios .smart-card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

.native-ios .smart-card-actions .smart-nav-primary {
  flex: 1 1 140px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  background: linear-gradient(135deg, #059669, #047857);
  color: #ffffff;
  border: 0;
  border-radius: 12px;
  padding: 11px 14px;
  font-size: 14px;
  font-weight: 700;
  box-shadow: 0 4px 12px rgba(5, 150, 105, 0.24);
}

.native-ios .smart-card-actions .smart-inline-detail-btn {
  flex: 1 1 100%;
  background: #f8fafc;
  color: #334155;
  border: 1px solid #cbd5e1;
  border-radius: 10px;
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 600;
}

.native-ios .smart-inline-details {
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px dashed #cbd5e1;
}
"""


def patch_native_css(content: str) -> tuple[str, list[str], list[str]]:
    if "smart-inline-details" in content:
        return content, [], ["native.css 已包含无地图单栏卡片式 iOS 原生样式"]
    return (
        NATIVE_CSS_NEW,
        [
            "升级 native.css：采用毛玻璃吸顶导航栏、圆角卡片分组、全宽无地图单栏布局与主次分明的『高德地图导航』主按钮样式",
        ],
        [],
    )


# ============================================================================
# 主执行器
# ============================================================================

PATCH_PIPELINE = [
    ("frontend/ios/App/App/Info.plist", patch_info_plist),
    ("frontend/src/lib/platform.ts", patch_platform_ts),
    ("frontend/src/App.vue", patch_app_vue),
    ("frontend/src/components/SmartPlanner.vue", patch_smart_planner),
    ("frontend/src/native.css", patch_native_css),
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
        description="AlongRoute iOS 端产品化升级一键脚本 (去地图 + 原生拉起高德地图 + 移除开发者调试)"
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
    print(f"\n=== AlongRoute iOS 端产品化升级摘要 ({mode_label}) ===")
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