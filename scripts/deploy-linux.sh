#!/usr/bin/env bash
# Run in a normal SSH terminal; installs only the AlongRoute service.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
port="${ALONGROUTE_PORT:-8080}"
bind_host="${ALONGROUTE_HOST:-0.0.0.0}"
[[ $EUID -eq 0 ]] || { echo '请使用 root 或 sudo bash scripts/deploy-linux.sh'; exit 1; }
[[ $port =~ ^[0-9]+$ ]] && ((port > 0 && port <= 65535)) || { echo '端口无效'; exit 1; }
[[ $bind_host =~ ^[a-zA-Z0-9.:_-]+$ ]] || { echo '监听地址无效'; exit 1; }
[[ $project_dir != *[[:space:]\"%]* ]] || { echo '项目路径不能包含空白、双引号或百分号'; exit 1; }
command -v python3 >/dev/null
command -v systemctl >/dev/null
python3 - <<'PY'
import socket
try:
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0))
except OSError as exc:
    raise SystemExit(f'无法创建监听端口，请从普通SSH终端执行此脚本：{exc}')
PY
unit_file=/etc/systemd/system/alongroute.service
if [[ -f $unit_file ]] && ! grep -q '^# Managed by AlongRoute deploy-linux.sh$' "$unit_file"; then
    echo '已有同名服务并非本脚本创建，请先检查 /etc/systemd/system/alongroute.service'; exit 1
fi
mkdir -p .runtime/releases
exec 9>.runtime/deploy.lock
flock -n 9 || { echo '另一个部署正在执行'; exit 1; }

# Reuse the dependencies already installed by the owner, if complete.
deps_dir="$project_dir/.runtime/test-deps"
if ! PYTHONPATH="$deps_dir" python3 -c 'import fastapi,uvicorn,uvloop,httpx,dotenv' 2>/dev/null; then
    deps_dir="$project_dir/.runtime/server-deps"
    if ! PYTHONPATH="$deps_dir" python3 -c 'import fastapi,uvicorn,uvloop,httpx,dotenv' 2>/dev/null; then
        python3 -m pip install --index-url https://pypi.org/simple --target "$deps_dir" -r backend/requirements.txt
    fi
fi

# Keep Node local to the project; verify the archive against official checksums.
if ! command -v node >/dev/null || ! node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 22 ? 0 : 1)'; then
    if [[ ! -x .runtime/node/bin/node ]]; then
        case "$(uname -m)" in
            x86_64) node_arch=x64 ;;
            aarch64|arm64) node_arch=arm64 ;;
            *) echo '暂只支持 x86_64 / arm64 Linux'; exit 1 ;;
        esac
        node_base=https://nodejs.org/dist/latest-v22.x
        curl -fsSL --retry 2 "$node_base/SHASUMS256.txt" -o .runtime/node-checksums.txt
        read -r expected node_archive < <(awk -v arch="$node_arch" '$2 ~ ("^node-v22[.][0-9]+[.][0-9]+-linux-" arch "[.]tar[.]xz$") {print $1, $2; exit}' .runtime/node-checksums.txt)
        [[ $expected =~ ^[a-fA-F0-9]{64}$ && -n $node_archive ]] || { echo '无法解析Node官方校验清单'; exit 1; }
        curl -fSL --retry 2 "$node_base/$node_archive" -o ".runtime/$node_archive"
        printf '%s  %s\n' "$expected" ".runtime/$node_archive" | sha256sum -c -
        mkdir -p .runtime/node
        tar -xJf ".runtime/$node_archive" --strip-components=1 -C .runtime/node
    fi
    export PATH="$project_dir/.runtime/node/bin:$PATH"
fi
[[ -f backend/.env ]] || cp backend/.env.example backend/.env
[[ -f frontend/.env.local ]] || cp frontend/.env.example frontend/.env.local
chmod 600 backend/.env frontend/.env.local
release_dir="$project_dir/.runtime/releases/$(date -u +%Y%m%dT%H%M%S)-$$"
mkdir -p "$release_dir"
(
    cd frontend
    npm ci --registry=https://registry.npmjs.org --cache "$project_dir/.runtime/npm-cache"
    npm run build -- --outDir "$release_dir/web"
)
[[ -s $release_dir/web/index.html ]]
PYTHONPATH="$deps_dir:$project_dir/backend" ALONGROUTE_STATIC_DIR="$release_dir/web" python3 - <<'PY'
from app.production import create_app
create_app()
print('部署入口检查通过')
PY
# Build and imports succeeded before touching a running service.
old_unit="$release_dir/previous.service"
[[ ! -f $unit_file ]] || cp "$unit_file" "$old_unit"
cat > "$release_dir/alongroute.service" <<UNIT
# Managed by AlongRoute deploy-linux.sh
[Unit]
Description=AlongRoute web application
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$project_dir
Environment="PYTHONPATH=$deps_dir:$project_dir/backend"
Environment="ALONGROUTE_STATIC_DIR=$release_dir/web"
ExecStart=/usr/bin/python3 -m uvicorn app.production:create_app --factory --host $bind_host --port $port --workers 1 --loop uvloop --no-proxy-headers --limit-concurrency 32
Restart=on-failure
RestartSec=3
TimeoutStopSec=120
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
install -m 644 "$release_dir/alongroute.service" "$unit_file"
systemctl daemon-reload
systemctl enable alongroute.service
systemctl restart alongroute.service
healthy=false
for attempt in {1..20}; do
    if systemctl is-active --quiet alongroute.service && \
       curl --noproxy '*' -fsS --max-time 2 -D "$release_dir/health.headers" "http://127.0.0.1:$port/api/health" > "$release_dir/health.json" && \
       grep -Fqi "x-alongroute-release: ${release_dir##*/}" "$release_dir/health.headers" && \
       curl --noproxy '*' -fsS --max-time 2 "http://127.0.0.1:$port/" -o /dev/null; then
        healthy=true; break
    fi
    sleep 1
done
if [[ $healthy != true ]]; then
    journalctl -u alongroute.service -n 30 --no-pager
    if [[ -f $old_unit ]]; then
        install -m 644 "$old_unit" "$unit_file"
        systemctl daemon-reload
        systemctl restart alongroute.service
        echo '新版本健康检查失败，已恢复旧服务配置。'
    else
        systemctl stop alongroute.service
        systemctl disable alongroute.service
        echo '健康检查失败，已停止新服务；请查看上方日志。'
    fi
    exit 1
fi
ln -sfn "$release_dir" .runtime/current-release
printf '\n部署成功：监听 %s:%s，浏览器访问 http://服务器公网IP:%s/\n' "$bind_host" "$port" "$port"
curl --noproxy '*' -fsS "http://127.0.0.1:$port/api/config"
printf '\n日志：journalctl -u alongroute.service -f\n'
