# Linux 服务器部署

在服务器的普通 SSH 终端中执行，默认通过公网 IP 的 8080 端口访问：

```bash
cd /root/AlongRoute/AlongRoute
bash scripts/deploy-linux.sh
```

脚本需要 root 和 systemd，以及 Python 3、curl、tar、xz、sha256sum、flock。普通 SSH 终端需要能访问 PyPI、npm 和 Node.js 下载站；受限的助手命令沙箱可能不具备联网、端口监听或 systemd 管理权限。

部署过程：

- 优先复用已安装的 `.runtime/test-deps`；缺少依赖时在 `.runtime/server-deps` 安装后端依赖，显式使用官方 PyPI HTTPS 源。
- 系统 Node.js 版本不足22时，将官方 Node.js 22 Linux 包下载至项目 `.runtime/node`，核对SHA256；不更改系统 Node.js。
- 通过锁文件安装前端依赖并执行类型检查、生产构建，产物放入独立 `.runtime/releases/` 目录。
- 构建和导入检查成功后才更新 `/etc/systemd/system/alongroute.service`，启动单进程Uvicorn，前端静态文件和API同源服务。
- 验证健康接口、版本标记和首页，失败时恢复此前由本脚本管理的服务配置。已有其他来源的同名服务不会被覆盖。

阿里云实例安全组及服务器防火墙需允许预期访问来源到 **TCP 8080**；脚本不会自动修改它们。完成后访问 `http://服务器公网IP:8080/`。脚本的本机健康检查不代表公网安全组已经放行。

## 演示与真实地图

首次部署会在缺失时复制空白 `.env` 模板，不覆盖现有配置。当前没有地图Key时启用演示模式。

真实规划由服务器所有者填写：

- `backend/.env`：`AMAP_WEB_SERVICE_KEY`、`AMAP_JS_SECURITY_CODE`。
- `frontend/.env.local`：仅 `VITE_AMAP_JS_KEY`（浏览器公开的JS Key），不能填入后端密钥。

高德控制台需允许实际访问地址。修改前端JS Key后重新运行部署脚本以重建；仅修改后端配置时重启服务即可。

## 本机 OSRM

2026-11-09 已接入现有 `osrm-guangdong` 容器：镜像 `ghcr.io/project-osrm/osrm-backend:v5.27.1`，CH + mmap，table 容量200，宿主机路网目录 `/root/osrm-guangdong`。AlongRoute 的 `backend/.env` 使用：

```dotenv
OSRM_BASE_URL=http://127.0.0.1:5000
CANDIDATE_STRATEGY=hub_first
OSRM_DATA_VERSION=guangdong-car-ch-v5.27.1-hsgr20261109T031719
```

版本标记使用已观察到的索引文件构建时间，不表示 OSM 原始数据日期。没有改动现有 OSRM 容器或路网。

已用应用坐标转换和矩阵适配器验证两个固定的路网吸附测试点，两个候选均返回 `engine=osrm`，该次矩阵查询约26毫秒；仅验证连接与接口，不代表真实行程的召回质量或延迟分位数。随后重启 `alongroute.service`，服务 active/running，8080健康接口返回HTTP200。后端配置已生效；前端静态发布目录仍沿用现有版本。

如 OSRM 地址变更，更新后端 `.env` 后重启服务。服务未配置或矩阵失败时，候选回退几何粗排，高德最终核验仍执行。

## 运行维护

```bash
systemctl status alongroute.service --no-pager
journalctl -u alongroute.service -f
systemctl restart alongroute.service
systemctl stop alongroute.service
curl --noproxy '*' http://127.0.0.1:8080/api/health
curl --noproxy '*' http://127.0.0.1:8080/api/config
```

重新运行部署脚本即可更新。旧构建保留在 `.runtime/releases/`；失败更新不会删除旧构建。`current-release` 指向最后通过健康检查的版本。

可以在执行时设置 `ALONGROUTE_PORT` 和 `ALONGROUTE_HOST` 修改监听配置，默认 `0.0.0.0:8080`。外部反向代理需要另外配置，本服务不信任客户端转发的IP头。

公网入口有进程内限流：同一来源每分钟最多6次规划、60次其他API、180次地图代理请求，全局每分钟最多600次API/地图请求；最大并发连接由Uvicorn限制为32。它们是单进程运行保护，不提供用户认证或付费配额隔离。上线真实地图前应按实际使用人数和Key配额调整。

## 本轮验证状态

部署入口使用临时静态文件与ASGI请求进行离线测试，覆盖页面/资源、API、缺失构建、路径边界、地图代理规则及限流。用户已从普通SSH终端完成前端类型检查、生产构建及systemd部署，成功发布版本 `20261106T095825-88458`。已核对该版本健康记录为HTTP200、status=ok，首页检查通过，current-release标记正确。当前为演示模式，地图代理未配置。尚未从服务器外部核验公网8080端口与安全组。


2026-11-07更新：已部署候选可视化回放版本 `20261107T021325-177398`，监听 `0.0.0.0:8080`。systemd运行正常，健康及回放接口HTTP200，真实地图模式与代理配置保持生效。生产页面已完成Chromium桌面及390px手机验证，五阶段回放、详情、筛选和错误提示通过，未触发新路线规划；本轮未从服务器外部核验公网入口。


2026-11-12更新：已发布 `20261111T232424Z-planning-notes`，用户页移除算法与预算长提示，开发调试页新增本次规划摘要与可展开诊断。服务 active，健康、`/user`、`/debug` 和静态资源校验通过，地图模式 live 且代理就绪。前端构建、303项后端测试及1440/800/390像素浏览器检查通过。浏览器验证使用模拟规划响应，未触发真实地图规划，未从外部网络验证公网入口。
