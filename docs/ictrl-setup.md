# iCTRL 安装与配对（iPhone 侧，一次性 + 每次重启）

来源：`OpenMinis/iCTRL`。iCTRL 是跑在 iPhone 上的 XCUITest runner，
对外提供 **JSON-RPC 2.0 over HTTP，端口 8200**，请求加密（X25519 配对 +
HMAC-SHA256 信封）。`GET /skill` 是操作手册，`GET /schema` 是方法清单。

> ⚠️ 安全：iCTRL 把手机的完全控制权交给持有配对密钥的一方。
> 仅在自有设备/账号上使用，只批准自己发起的配对，用完关闭远程访问。
> 不要让它无人监管地接触支付、消息等不可撤销操作。

## 前提（两件事都必须满足）

1. **能签名并安装 IPA**：免费 Apple ID 用最新版 **SideStore**；付费账号用
   `Scripts/sign-ipa.sh` 或 Xcode。（runner 内嵌 `PlugIns/iCTRLUITests.xctest`，
   必须从内到外单独签名，普通签名器会漏签导致启动即崩溃。）
2. **挂载 Developer Disk Image (DDI)**：XCUITest 依赖 `testmanagerd`，只有挂载
   DDI 时才存在。**每次重启都要重新挂载**。有 Mac+Xcode 可自动挂载；没有则用
   StikDebug 或 iCTRL App 在设备上挂载。

先开 **设置 → 隐私与安全性 → 开发者模式**，重启。

## 安装

- **免费 Apple ID**：iLoader 装 SideStore → App Store 装 **LocalDevVPN** 并连接
  → SideStore → My Apps → `+` → 选 `iCTRLUITests-Runner.ipa`
  → 设置 → 通用 → VPN 与设备管理 → 信任证书。
  （免费证书 7 天到期，需用 SideStore 刷新。）
- **付费账号**：`Scripts/sign-ipa.sh` 签名，或 Xcode 直接跑。

## 启动 runner

1. 在手机上启动 iCTRL runner（**真正干活的这个**）。
2. 允许**通知**和**本地网络**权限。
3. runner 的通知里长按可：✅ 批准配对 / 🌐 开启 Remote Access / 🛑 停止服务。

> 默认只监听 `127.0.0.1`。主机不在手机上时，必须打开 **🌐 Remote Access**，
> 且主机与手机在**同一 Wi-Fi**。蜂窝网络下 LAN 不可达。

## 主机侧配对

```bash
cd host
bridge pair --host <手机IP>:8200
# 手机弹出配对通知 → 点 Allow
bridge doctor            # 验证 device_info / skill_version
```

配对密钥缓存在 `~/.ictrl/token.json`，**按 host:port 存**（DHCP 换 IP 需要重配）。
密钥 30 天未用会过期；只有真的返回 `401` 才重新配对。

## 交给 Hermes

iCTRL 自带 skill，直接把它装成 Hermes skill（见 `host/bridge/cli.py:install-skill`），
然后对 Hermes 说：

> "我的 iPhone 在 iCTRL 上运行，地址 `<IP>:8200`，想办法驱动它。"

Hermes 会读 `GET /skill`、`GET /schema`，从设备下载客户端，发起配对。

## 排错

| 现象 | 处理 |
|---|---|
| `/schema` 打不开 | runner 没在跑，让用户启动（DDI 每次重启会丢） |
| `401` | 密钥失效，重新 `pair` |
| UI 调用连续超时 | `reset`，然后每几秒轮询 `logs` 直到卡住的调用结束（15–45s），别发长 `sleep` |
| 屏幕锁屏 | `unlock` 只点亮屏幕；需要密码时要用户处理 |
| 只有 `logs`/`config`/`reset` 有响应 | UI 队列被卡住的调用占住，正常——用它诊断 |

## iLoader 登录 Apple ID 失败

**先确认版本：必须用 `2.3.8` 或更新。**
`2.3.7` 存在已知 bug，登录时会对 `gsa.apple.com` 返回
`503 Service Temporarily Unavailable`；`2.3.8` 已修复（见其 release notes）。

- `2.3.8` 起 **自定义 anisette 服务器不再可用**，会自动改用内置的两个之一。
- `2.3.8` 登录会**要求重新做一次 2FA**：受信任设备 2FA 和未开 2FA 的账号正常，
  **短信（SMS）2FA 仍不可用**。

### 症状 A：`unknown session (-45025)` / `Anisette provisioning failed`

公用 anisette 服务器不稳定（`ani.sidestore.io` 时会 502）。
**仅 `2.3.7` 及更早**可用下面方式自建本地服务：

```bash
colima start                     # 重启后需重新启动 colima
docker run -d --name anisette -p 6969:6969 \
  --restart unless-stopped dadoum/anisette-v3-server
```

iLoader → Settings → **Anisette Server** → 自定义填 **`http://127.0.0.1:6969`**
（带 `http` 前缀会被原样使用，否则 iLoader 会补 `https://`）→ 再点
**Reset anisette state** → 重新登录。

### 症状 B：`HTTP 503 ... gsa.apple.com/grandslam/GsService2`

- **首选：升级到 iLoader `2.3.8`**（专门修这个）。
- 若仍失败，检查是否走了代理：`gsa.apple.com` 经机房/VPN 出口 IP 会被 Apple 拒。
  临时关掉系统代理再登录，或把 `*.apple.com` 加入代理例外。
- 连续失败可能触发 Apple 临时限流，等 10–15 分钟再试。

### 症状 C：`2.3.8` 报 `Failed to request /v3/client_info` / `error sending request`

`2.3.8` 默认用内置 anisette 服务器 `ani.jkcoxson.com`（另一个是
`ani.stikstore.app`）。实测这台机器上该服务器**直连稳定（0.7–2s）**，
但**经 Clash 代理又慢（3–5s）又间歇性失败（连接直接断）**，导致
`error sending request`。

→ **关掉 Clash 系统代理**（iLoader 从 Finder 启动，读的是 macOS 系统代理），
或把 `ani.jkcoxson.com`、`ani.stikstore.app` 加进代理例外。

验证命令：
```bash
for i in 1 2 3; do
  curl -s -o /dev/null -w "直连 %{http_code}/%{time_total}s\n" --noproxy '*' https://ani.jkcoxson.com/v3/client_info
  curl -s -o /dev/null -w "代理 %{http_code}/%{time_total}s\n" -x http://127.0.0.1:7892 https://ani.jkcoxson.com/v3/client_info
done
```

> 备选公用 anisette 服务器（不稳定）：`ani.stikstore.app`、`ani.sidestore.zip`、
> `ani.xu30.top`、`anisette.wedotstud.io`。
