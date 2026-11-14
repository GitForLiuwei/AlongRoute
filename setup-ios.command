#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo '请在安装 Xcode 的 Mac 上运行此文件。'
  exit 1
fi
if ! command -v node >/dev/null || ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)'; then
  echo '请先安装 Node.js 22 或更高版本：https://nodejs.org/'
  exit 1
fi
if ! xcodebuild -version; then
  echo '请先安装并打开完整 Xcode，完成初次安装和许可确认，再重新运行。'
  exit 1
fi
npm --prefix frontend ci
echo '正在生成 iOS 应用并打开 Xcode，实际配置文件和后端地址如下。'
echo 'Mac 配置保存在用户目录中，更新或替换源码后会继续使用。'
echo '连接 Mac 本地后端时，请另行运行 start-backend-macos.command；连接远程服务器时无需启动。'
npm --prefix frontend run ios:open
