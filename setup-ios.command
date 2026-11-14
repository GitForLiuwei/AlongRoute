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
[[ -f frontend/.env.ios.local ]] || cp frontend/.env.ios.example frontend/.env.ios.local
npm --prefix frontend ci
echo '正在生成 iOS 应用并打开 Xcode。默认连接 Mac 的 http://localhost:8000。'
echo '请另行运行 start-backend-macos.command 启动本地规划服务。'
echo '真机运行前，请修改 frontend/.env.ios.local 中的服务地址为 Mac 的局域网 IP，并重新运行本脚本。'
npm --prefix frontend run ios:open
