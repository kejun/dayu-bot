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

## iLoader 登录 Apple ID 失败（anisette）

症状：`Failed to log in to Apple ID` → `Failed to get anisette data` →
`Anisette provisioning failed` → `unknown session (-45025)`。

原因：公用 anisette 服务器不稳定（`ani.sidestore.io` 时会 502），或代理干扰了
`gsa.apple.com` 的会话。**解决：本地自建 anisette 服务。**

```bash
colima start                     # 重启后需要重新启动 colima
docker run -d --name anisette -p 6969:6969 \
  --restart unless-stopped dadoum/anisette-v3-server
```

iLoader → Settings → **Anisette Server** → 自定义填 **`http://127.0.0.1:6969`**
（带 `http` 前缀会被原样使用，否则 iLoader 会补 `https://`）→ 再点
**Reset anisette state** 清掉坏会话 → 重新登录。

若仍失败，临时关闭系统代理（Clash）再试：Apple 认证流量被代理接管会导致
会话不一致。

> 备选公用服务器（不稳定）：`ani.stikstore.app`、`ani.sidestore.zip`、
> `ani.xu30.top`、`ani.wedotstud.io`。
