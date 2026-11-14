# 在 Mac 上生成和运行 iOS 应用

现有 Vue 界面通过 Capacitor 7 打包为 iOS 应用，前端资源随安装包保存。定位、复制和分享调用 iOS 原生能力；路线规划、高德安全代理和实时事件流仍由 FastAPI 提供，Python 后端不打包进手机。

## 1. 准备 Mac

- 安装完整 **Xcode 16 或更新版本**，首次打开并完成许可和组件安装；在 Settings → Components 安装 iOS 模拟器。
- 安装 **Node.js 22+**；本地启动后端还需要 **Python 3.11+**。
- 应用最低支持 **iOS 16**。工程使用 Swift Package Manager，不需要安装 CocoaPods。首次在 Xcode 中打开时需要联网获取 Swift 依赖。

将整个项目复制到 Mac，或解压提供的 `AlongRoute-iOS-source.zip`。以下命令均在包含 `frontend`、`backend` 的项目根目录执行。

## 2. 启动规划服务

在第一个终端运行（保持窗口打开）：

```bash
bash start-backend-macos.command
```

脚本创建 Python 虚拟环境、安装依赖并启动端口 8000。打开 `http://localhost:8000/api/health`，应显示 `status: ok`。

没有高德配置时，后端运行演示模式。真实地点搜索和路线需要在 `backend/.env` 中设置 `AMAP_WEB_SERVICE_KEY`；地图安全代理还需 `AMAP_JS_SECURITY_CODE`。修改后重启后端。使用已部署的后端时，可以跳过本地后端启动；远端服务需要包含本次新增的 iOS CORS 配置。

### 连接已部署的 Linux 后端

手机可以直接连接服务器，Mac 不需要一直运行本地 FastAPI。先创建并编辑配置：

```bash
cp -n frontend/.env.ios.example frontend/.env.ios.local
open -e frontend/.env.ios.local
```

将 `VITE_API_BASE_URL` 设置为服务器根地址，不带 `/api`，同时在同一个文件填写 `VITE_AMAP_JS_KEY`：

```dotenv
VITE_API_BASE_URL=https://你的后端域名
VITE_AMAP_JS_KEY=你的高德Web端JS_API_Key
```

当前项目服务器的 Debug 调试入口是 `http://120.24.222.150:8080`，可用于现有 Xcode Debug 构建。2026-11-14 已验证公网 `/api/config` 返回 `mode: live`、`map_proxy_ready: true`，并且规划接口的 iOS CORS 预检通过。该地址没有配置 HTTPS，正式分发时应改为带有效证书的 HTTPS 服务。

后端需要部署本次 iOS 兼容代码，并允许 `CORS_ALLOW_ORIGINS=capacitor://localhost`。服务器上的 `backend/.env` 保存 `AMAP_WEB_SERVICE_KEY` 和 `AMAP_JS_SECURITY_CODE`，手机端仅保存公开的 JS Key。服务器环境变量修改后重启后端；前端地址或 Key 修改后重新 `ios:sync` 并在 Xcode 安装。

可先在 iPhone Safari 打开服务器的 `/api/health` 和 `/api/config`，检查公网连接与配置状态。Safari 能打开接口不代表 WebView 跨域一定通过，仍需检查 CORS 响应。通过服务器连接时，手机和 Mac 无需位于同一局域网。

## 3. 生成并打开 Xcode 工程

在第二个终端运行：

```bash
bash setup-ios.command
```

脚本检查 Mac / Node / Xcode、安装 npm 锁定依赖、创建 `frontend/.env.ios.local`，构建网页资源，同步 iOS 插件并打开 Xcode。

默认服务地址为 `http://localhost:8000`，用于 **Mac 上的 iOS 模拟器**。在 Xcode 顶部选择 **App** scheme 和一个 iPhone 模拟器，点击 ▶ 或按 **⌘R**。不需要设置 Apple 签名团队即可运行模拟器。

若出现 Xcode 命令行工具目录错误，安装完整 Xcode 后执行：

```bash
sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer
sudo xcodebuild -runFirstLaunch
```

如双击 `.command` 文件没有执行权限，可直接使用上述 `bash` 命令。

### npm 安装出现证书错误

出现 `UNABLE_TO_GET_ISSUER_CERT_LOCALLY` 表示依赖下载未通过证书校验，持续等待不能解决。先确认 Node 版本、npm 自定义证书配置，以及 curl 实际使用的受信任证书库：

```bash
node -v
npm config get cafile
curl -vI --max-time 15 https://registry.npmjs.org/ 2>&1 | grep -E 'CAfile:|CApath:|issuer:|SSL certificate verify'
```

如果 curl 显示 `CAfile: /etc/ssl/cert.pem` 且 `SSL certificate verify ok.`，可以让 Node 加载该文件：

```bash
NODE_EXTRA_CA_CERTS=/etc/ssl/cert.pem npm ping --fetch-retries=0 --fetch-timeout=15000
```

显示 `PONG` 后，再运行：

```bash
NODE_EXTRA_CA_CERTS=/etc/ssl/cert.pem bash setup-ios.command
```

此方式已在 Node.js 23.11.0 / Xcode 26.3 的 Mac 环境验证通过，保留 HTTPS 校验。其他电脑应使用自己已验证的 CA 文件路径；企业代理需要其受信任 CA。`NODE_USE_SYSTEM_CA` 环境变量的支持范围取决于 Node 版本，不能假定所有 Node 22+ 都识别。

只复制代码块中的命令，不要复制终端的 `(base) 用户名@电脑名 项目目录 %` 提示符。安装成功后，npm 的弃用和漏洞提示不等于构建失败；不要直接执行 `npm audit fix --force`，应先检查升级对现有依赖的兼容性。

## 4. 在自己的 iPhone 上运行

以下第 1–2 步用于连接 Mac 本地后端。连接服务器时，使用上文的服务器配置，然后从第 3 步继续。

1. iPhone 和 Mac 连接同一局域网；从 Mac 系统设置 → Wi-Fi → 详细信息 → TCP/IP 获取 Mac 的 IP。
2. 修改 `frontend/.env.ios.local`：

   ```dotenv
   VITE_API_BASE_URL=http://192.168.1.10:8000
   VITE_AMAP_JS_KEY=你的高德Web端JS公开Key
   ```

   将示例 IP 换成 Mac 的实际 IP。真机的 `localhost` 指向手机自身，无法访问 Mac。服务地址必须是根地址，不带 `/api`。也可填写已部署服务器的 HTTPS 根地址。
3. 再次运行 `bash setup-ios.command`。以后修改页面或 iOS 环境配置，也要重新同步；只在 Xcode 中点击运行不会自动重建网页。
4. 用数据线连接 iPhone，信任电脑。iOS 16+ 按系统提示开启「设置 → 隐私与安全性 → 开发者模式」。
5. Xcode 中选择 App target → Signing & Capabilities，启用 Automatically manage signing，选择自己的 Apple ID / Team。将 Bundle Identifier 从 `com.alongroute.app` 改成你的唯一标识，例如 `com.yourname.alongroute`；同时修改 `frontend/capacitor.config.ts` 中的 `appId` 以保持一致。
6. 顶部设备选择你的 iPhone，点击 ▶。按提示允许应用访问本地网络；主动点击当前位置时再允许定位。

可先在 iPhone 的 Safari 打开 `http://Mac的IP:8000/api/health`，确认网络和 Mac 防火墙允许访问。Mac 休眠、后端退出、切换网络或 IP 变化都会影响本地服务连接。

免费 Apple ID 可用于个人真机调试；安装有效期和设备额度受 Apple 限制。通过 App Store / TestFlight 分发需要相应 Apple Developer 资格及签名。

## 5. 日常开发命令

```bash
# 仅首次或依赖变化后
npm --prefix frontend ci

# 首次手动配置（setup-ios.command 已自动处理；已有文件请保留）
cp -n frontend/.env.ios.example frontend/.env.ios.local

# 重新构建 iOS 资源并同步到原生工程
npm --prefix frontend run ios:sync

# 构建、同步并打开 Xcode（仅 Mac）
npm --prefix frontend run ios:open
```

工程位于 `frontend/ios/App/App.xcodeproj`。`ios:sync` 也可在 Linux 上生成资源和插件配置，但编译、模拟器运行、签名和导出 IPA 必须使用 Mac / Xcode。

浏览器开发继续使用 `npm --prefix frontend run dev`，默认走 Vite 同源代理。iOS 构建额外加载 `.env.ios.local`，将 API、SSE 和 `/_AMapService` 统一指向配置的后端。不要在 `VITE_` 变量中放置 Web 服务 Key 或 JS 安全密钥。

## 6. 地图与打包说明

- 需要申请 **Web端（JS API）Key** 并填入 `frontend/.env.ios.local` 的 `VITE_AMAP_JS_KEY`。高德「iOS 平台」Key 仅用于原生高德 SDK，本工程尚未接入，不能填到现有 JS Key 字段。Web服务 Key 和 JS 安全密钥分别填写后端的 `AMAP_WEB_SERVICE_KEY`、`AMAP_JS_SECURITY_CODE`。
- `.env.ios.local` 会覆盖通用 `.env.local` 的同名配置，包括空值。手机出现「尚未配置高德地图」说明安装包中的 JS Key 为空；检查 iOS 专用配置，再执行 `npm --prefix frontend run ios:sync` 并按 ⌘R 重新安装。
- 高德底图仍使用 Web JS SDK，需要有效的 JS Key、后端安全代理，以及允许对应 WebView 来源的高德 Key 配置。应用页面来源为 `capacitor://localhost`。若高德控制台拒绝这一来源，需要根据高德当前政策调整 Key 授权或后续改用原生地图 SDK；需在真机验证地图、导航跳转和真实路线。
- 后端默认只允许 iOS 来源 `capacitor://localhost` 跨域访问。自定义 hostname 时同步调整 `CORS_ALLOW_ORIGINS` 并重启服务。外置反向代理需转发 CORS 预检并允许 SSE 长连接。
- Debug 构建使用 `Info-Debug.plist`，允许 WebView 通过 HTTP 访问局域网服务；Release 使用 `Info.plist`，保留系统传输安全限制。**正式分发前将 `VITE_API_BASE_URL` 换成 HTTPS 服务地址，重新 `ios:sync` 后再 Archive。**
- 定位只在用户主动操作时获取，未开启后台持续定位。前端仍按原有逻辑将系统 WGS84 坐标转换为高德 GCJ02。
- 导出安装包：选择真实设备或 Any iOS Device → Product → Archive → Organizer → Distribute App，使用你的签名与描述文件导出或上传。普通 ZIP 工程文件不是可直接安装的 IPA。

2026-11-14 验证记录：Linux 上前端类型检查、iOS 资源构建和三个原生插件同步通过，URL 配置测试 3 项、后端 API / CORS / SSE / 生产入口专项测试 25 项通过。Mac 已完成 Xcode 26.3 工程打开，用户提供的真机截图确认界面可运行；当时 JS 地图 Key 尚未打包配置，不能据此证明地图加载、定位、分享和真实路线均已通过。完整真机验收、Archive 和签名仍需在 Mac 上完成。
