# 仅需你自己完成：高德地图 Key 配置（第 5 步）

1. 登录 [高德开放平台控制台](https://console.amap.com/)，创建应用 `AlongRoute`。
2. 创建 **Web 服务 API Key**（供 Python 后端调用路径规划、POI、输入提示）。
3. 创建 **Web端（JS API）Key** 和对应 **安全密钥 securityJsCode**（供浏览器显示地图；安全密钥存后端）。
4. 如果 Key 有域名白名单，按控制台规则允许 `localhost` / `127.0.0.1`。公网部署以后再更新为正式域名。
5. 首次运行 `setup-windows.ps1` 后，编辑以下文件：

`backend/.env`：

```dotenv
AMAP_WEB_SERVICE_KEY=你自己的Web服务Key
AMAP_JS_SECURITY_CODE=你自己的JS_API安全密钥
```

`frontend/.env.local`：

```dotenv
VITE_AMAP_JS_KEY=你自己的Web端JS_API_Key
```

6. 重启 `start-backend.ps1` 和 `start-frontend.ps1` 两个终端。
7. 浏览器打开 http://127.0.0.1:5173/；应从“体验演示模式”切换成“真实路线模式”，并可用搜索结果选择地点。

**安全规则：** 不要在聊天中发送 Key，不要将 JS API 安全密钥写到前端，不要把 `.env` 提交到 GitHub。前端 Web JS Key 可以被用户看到，因此应使用高德允许的域名限制和配额设置进行保护。

**常见问题：**
- 地图空白但能搜索：检查 JS Key、安全密钥、代理和浏览器 F12 网络/控制台。
- 地图能打开但无候选：检查 Web 服务 Key、配额，尝试将绕路限制提高到 10 分钟。
- 公交没有结果：选择有 citycode 的地点搜索建议，确认是否覆盖该区域、是否仍有运营的公交线路。
- 无法算路：检查服务器能否访问 `restapi.amap.com`、坐标是否中国大陆 GCJ-02 坐标。

官方说明：[高德 JS 安全密钥](https://developer.amap.com/api/javascript-api-v2/guide/abc/jscode)。
