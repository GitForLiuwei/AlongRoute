#!/usr/bin/env python3
r"""AlongRoute iOS 端纯原生拉起高德地图 App 修复脚本 (针对 commit e4417d7).

问题根因分析:
    在刚才的提交 `e4417d7` 中，虽然 `platform.ts` 已经有了 `openAmapNavigation` 和 `iosamap://` 构造逻辑，
    但由于缩进差异（14 空格 vs 16 空格），以下 3 处关键调用没有被替换成功，导致按钮点击时依然走的是 `https://uri.amap.com` 网页版：
    1. `SmartPlanner.vue` 第 282 行的 `<div class="smart-card-actions">` 仍然是 `<a :href="navigationUrl(r)" target="_blank">导航</a>`；
    2. `SmartPlanner.vue` 第 81 行的 `navigationUrl(r)` 最后一个参数传了硬编码的 `false`（强制返回 `https://uri.amap.com` 网页版），
       而不是默认跟随 `isNativeApp` 返回 `iosamap://path?...`；
    3. `App.vue` 第 125~135 行的 `markerUrl(r)`、`navigationUrl(r)` 以及第 224 行的 `viewPlace()` 仍然硬编码了 `https://uri.amap.com`。

修复方案:
    1. 将 `SmartPlanner.vue` 中的 `navigationUrl(r)` 改为默认跟随 `isNativeApp`（在 iOS 端直接返回 `iosamap://path?...`）；
    2. 将 `SmartPlanner.vue` 的 `.smart-card-actions` 替换为调用 `startNavigation(r)` 的原生导航按钮，并在卡片内支持展开路线详情；
    3. 将 `App.vue` 的 `markerUrl`、`navigationUrl` 和 `viewPlace` 全部接入 `buildAmapMarkerUrl`、`buildAmapNavigationUrl` 和 `openAmapMarker`。

用法:
    python3 fix_ios_amap_native_scheme.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def fix_smart_planner(repo_dir: Path) -> list[str]:
    fpath = repo_dir / "frontend" / "src" / "components" / "SmartPlanner.vue"
    content = fpath.read_text(encoding="utf-8")
    applied = []

    # 1. Fix navigationUrl(r) so it does NOT pass `false` on native app, but passes `isNativeApp`
    old_nav_fn = "return buildAmapNavigationUrl(r.point, props.snapshot?.origin, props.snapshot?.driver_destination, false)"
    new_nav_fn = "return buildAmapNavigationUrl(r.point, props.snapshot?.origin ?? props.origin, props.snapshot?.driver_destination ?? props.driverDestination, isNativeApp)"
    if old_nav_fn in content:
        content = content.replace(old_nav_fn, new_nav_fn, 1)
        applied.append("修复 SmartPlanner.navigationUrl()：在 iOS 端直接返回 iosamap://path 原生协议而非 false (网页版)")

    # 2. Match `.smart-card-actions` regardless of 14-space or 16-space indentation
    card_actions_pat = re.compile(
        r'(?P<ind>[ \t]*)<div v-if="active && key\(active\) === key\(r\)" class="smart-card-actions" @click\.stop>\s*\n'
        r'(?P<ind2>[ \t]*)<button :disabled="!usable" @click="openShare"><Share2 :size="19" />发给司机确认此点</button>\s*\n'
        r'(?P=ind2)<a v-if="usable" :href="navigationUrl\(r\)"[^>]*><Navigation :size="19" />导航</a>\s*\n'
        r'(?P=ind2)<button v-else disabled class="smart-card-navigation"><Navigation :size="19" />导航</button>\s*\n'
        r"(?P=ind)</div>"
    )
    if card_actions_pat.search(content):
        def _repl_actions(m: re.Match) -> str:
            ind = m.group("ind")
            ind2 = m.group("ind2")
            return (
                f'{ind}<div v-if="active && key(active) === key(r)" class="smart-card-actions" @click.stop>\n'
                f'{ind2}<button type="button" class="smart-nav-primary" :disabled="!usable" @click="startNavigation(r)"><Navigation :size="18" />高德地图导航</button>\n'
                f'{ind2}<button type="button" :disabled="!usable" @click="openShare"><Share2 :size="18" />发给司机确认</button>\n'
                f'{ind2}<button v-if="isNativeApp" type="button" class="smart-inline-detail-btn" :aria-expanded="detailsOpen" @click="detailsOpen = !detailsOpen">{{{{ detailsOpen ? \'收起路线详情\' : \'查看路线详情\' }}}}</button>\n'
                f"{ind}</div>\n"
                f'{ind}<div v-if="isNativeApp && detailsOpen && active && key(active) === key(r)" class="smart-inline-details" @click.stop>\n'
                f'{ind2}<slot name="details" />\n'
                f"{ind}</div>"
            )

        content = card_actions_pat.sub(_repl_actions, content, count=1)
        applied.append("升级 SmartPlanner.vue 卡片操作区：将 <a target='_blank'> 网页跳转彻底替换为 @click='startNavigation(r)' 原生拉起高德地图按钮")

    fpath.write_text(content, encoding="utf-8")
    return applied


def fix_app_vue(repo_dir: Path) -> list[str]:
    fpath = repo_dir / "frontend" / "src" / "App.vue"
    content = fpath.read_text(encoding="utf-8")
    applied = []

    # 1. Upgrade markerUrl and navigationUrl in App.vue to use buildAmapMarkerUrl / buildAmapNavigationUrl
    marker_nav_pat = re.compile(
        r"function markerUrl\(r: Recommendation\) \{\s*\n"
        r"  return `https://uri\.amap\.com/marker\?[^`]+`\s*\n"
        r"\}\s*\n\s*"
        r"function navigationUrl\(r: Recommendation\) \{\s*\n"
        r"  const req = snapshot\.value\s*\n"
        r"  if \(!req\) return markerUrl\(r\)\s*\n"
        r"  const place = \(p: Place\) => `[^`]+`\s*\n"
        r"  // https://developer\.amap\.com/api/uri-api/guide/travel/route\s*\n"
        r"  return `https://uri\.amap\.com/navigation\?[^`]+`\s*\n"
        r"\}"
    )
    marker_nav_new = (
        "function markerUrl(r: Recommendation) {\n"
        "  return buildAmapMarkerUrl(r.point, isNativeApp)\n"
        "}\n\n"
        "function navigationUrl(r: Recommendation) {\n"
        "  const req = snapshot.value\n"
        "  return buildAmapNavigationUrl(r.point, req?.origin ?? origin.value, req?.driver_destination ?? driverDestination.value, isNativeApp)\n"
        "}"
    )
    if marker_nav_pat.search(content):
        content = marker_nav_pat.sub(lambda _: marker_nav_new, content, count=1)
        applied.append("升级 App.vue markerUrl() 与 navigationUrl()：在 iOS 端直接生成 iosamap:// 原生协议")

    # 2. Upgrade viewPlace() in App.vue to call openAmapMarker(active.value.point)
    view_place_pat = re.compile(
        r"function viewPlace\(\) \{\s*\n"
        r"  if \(!active\.value \|\| result\.value\?\.mode !== 'live' \|\| stale\.value \|\| expired\.value\) return\s*\n"
        r"  const \{ lon, lat \} = active\.value\.point\.point\s*\n"
        r"  const title = encodeURIComponent\(active\.value\.point\.name\)\s*\n"
        r"  window\.open\(`https://uri\.amap\.com/marker\?position=\$\{lon\},\$\{lat\}&name=\$\{title\}`, '_blank', 'noopener,noreferrer'\)\s*\n"
        r"\}"
    )
    view_place_new = (
        "function viewPlace() {\n"
        "  if (!active.value || result.value?.mode !== 'live' || stale.value || expired.value) return\n"
        "  openAmapMarker(active.value.point)\n"
        "}"
    )
    if view_place_pat.search(content):
        content = view_place_pat.sub(lambda _: view_place_new, content, count=1)
        applied.append("升级 App.vue viewPlace()：点击『在高德查看候选位置』直接调用 openAmapMarker 拉起高德地图 App")

    fpath.write_text(content, encoding="utf-8")
    return applied


def fix_platform_ts(repo_dir: Path) -> list[str]:
    fpath = repo_dir / "frontend" / "src" / "lib" / "platform.ts"
    content = fpath.read_text(encoding="utf-8")
    applied = []

    # Ensure openAmapNavigation and openAmapMarker do not have any setTimeout fallback to webUrl
    if "window.open(webUrl" in content and "setTimeout" in content:
        content = re.sub(
            r"export function openAmapNavigation\([\s\S]*?\n\}\n\nexport function openAmapMarker\([\s\S]*?\n\}",
            """export function openAmapNavigation(
  dropoff: GeoPointLike,
  origin?: GeoPointLike | null,
  driverDestination?: GeoPointLike | null,
): void {
  if (isNativeApp) {
    const schemeUrl = buildAmapNavigationUrl(dropoff, origin, driverDestination, true)
    window.location.assign(schemeUrl)
    return
  }
  const webUrl = buildAmapNavigationUrl(dropoff, origin, driverDestination, false)
  window.open(webUrl, '_blank', 'noopener,noreferrer')
}

export function openAmapMarker(place: GeoPointLike): void {
  if (isNativeApp) {
    const schemeUrl = buildAmapMarkerUrl(place, true)
    window.location.assign(schemeUrl)
    return
  }
  const webUrl = buildAmapMarkerUrl(place, false)
  window.open(webUrl, '_blank', 'noopener,noreferrer')
}""",
            content,
            count=1,
        )
        fpath.write_text(content, encoding="utf-8")
        applied.append("移除 platform.ts 中会导致跳网页版的 setTimeout 回退逻辑")

    return applied


def main() -> int:
    parser = argparse.ArgumentParser(description="AlongRoute iOS 端纯原生拉起高德地图修复脚本")
    parser.add_argument("--repo-dir", type=Path, default=Path.cwd(), help="AlongRoute 仓库根目录")
    args = parser.parse_args()

    repo_dir = args.repo_dir.resolve()
    if not (repo_dir / "frontend" / "src").exists():
        print(f"未在 {repo_dir} 找到 frontend/src，请在仓库根目录运行。", file=sys.stderr)
        return 1

    all_applied = []
    all_applied.extend(fix_platform_ts(repo_dir))
    all_applied.extend(fix_smart_planner(repo_dir))
    all_applied.extend(fix_app_vue(repo_dir))

    print("\n=== iOS 纯原生高德地图拉起修复结果 ===")
    if all_applied:
        for item in all_applied:
            print(f"  + {item}")
    else:
        print("  (所有修复项均已存在)")
    print("\n请接着运行 `npm --prefix frontend run ios:sync` (或 `./setup-ios.command`) 将最新前端产物同步到 Xcode 后重新运行 App！")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())