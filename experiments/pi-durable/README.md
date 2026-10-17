# Pi Durable 隔离验证

这里保留迁移前的评估原型，不会被 `bridge serve` 导入，也不修改设备固件。正式默认后端已迁移至 Pi，生产入口和维护流程见 [host/pi](../../host/pi/README.md)；下文描述隔离实验的范围。
所有测试数据库、录音转写、屏幕快照和原始报告都保存在忽略目录 `.runtime/pi-durable/`。
实验结果与迁移判断见 [评估记录](../../docs/pi-durable-validation.md)。

锁定官方 `@earendil-works/pi-durable` **1.3.0**，不是同名的第三方 subagents 插件。
官方将其标记为 experimental；正式接入必须锁版本并保留应用侧安全规则。

## 运行

先按项目 README 配置 Python 虚拟环境和 `.env`。测试复用 `Config.load()` 的有效千问配置，
密钥仅经过子进程管道传递，不写入报告、不增加生产环境变量。需要 Node >=22.19.0。

```sh
cd experiments/pi-durable
npm ci --ignore-scripts --no-audit --no-fund
npm test
npm run memory
npm run phone
npm run benchmark
npm run voice
npm run context
npm run runtime-cost
npm run memory-scale
```

`phone` 会打开真实手机上的淘宝并读屏，要求 Runner 授权有效、手机已解锁，测试时不要并行运行其他手机任务。
`benchmark` 和 `runtime-cost` 使用 `phone` 保存的真实页面与启动响应做重放，不操作手机。
各命令应顺序执行，避免并发云请求干扰延时测量。
`voice` 在临时本机端口启动测试 Bridge，使用真实千问 ASR、模型、TTS 和真实 iCTRL 读屏；
播放完成回执由虚拟终端发送，物理 ESP32 继续连接生产 Bridge。

## 边界与方法

- `runtime.mjs`：Pi Harness、唯一所有者 SQLite、任务状态、记忆 Documents、模型与手机工具保护。
- `host_rpc.py`：常驻 JSONL 子进程，复用生产配置、任务整理器和 `PhoneTools`。
  原型只允许打开应用、读屏、读取完整快照、提问和结束，不开放点击、输入、付款。
- `validate.mjs`：真实模型记忆验证；真实手机和固定页面重放的交错延时对比。
- `test/lifecycle.test.mjs`：取消、问答门控、状态持久化、去重、日期和不确定结果。
  强制终止子进程，验证 unsafe 工具不重放；safe 工具仍使用外部幂等凭据。
- `agent-process.mjs`、`voice_check.py`：通过现有 Bridge 协议验证澄清链路。
- `context-check.mjs`：真实模型压缩测试历史，保留原文记录和独立记忆。
- `runtime-cost.mjs`：固定两轮模拟模型、真实工具封装及本地 SQLite，隔离云端波动测本地成本。
- `memory-scale.mjs`：3324 条测试记忆的重开、筛选和上下文组装成本，不调用云端。

云端对比复用相同的有效模型、关闭思考、384 token 输出上限（实际以 `.env` 为准）、
相同的受限工具定义和生产系统提示词。两边使用同一份经生产整理器校验的计划；
整理耗时另列，执行耗时包含模型、IPC、手机/重放工具与 Pi 持久化，不包含 ASR、用户等待和播报。
各场景两边各预热一次，随后交替先后顺序。Direct 使用现有非流式 JSON，Pi 官方 provider 使用 SSE，
因此云端比较也是这两种实际接法的比较；不能把全部差异归因于 SQLite。
20 次每侧的 P95 只是初步门槛，不是线上分位数或统计显著性证明。

## 不能交给框架隐式处理的规则

1. 等待的取消不等于工作的取消：调用 `Conversation.abort()`，并等待现有手机 RPC 收尾。
2. 工具意图持久化不代表外部动作恰好执行一次。手机工具均为 unsafe；未确认结果阻断后续请求和动作。
   运行时在手机调用前提交 pending intent，结果确认后才清除。保护位不能只靠 hook 抛异常。
3. 提问与收音由 request ID、question sequence、audio ID 绑定；Pi 不负责物理播报回执、VAD、AEC。
4. 长期偏好、当天覆盖、任务覆盖分开保存；覆盖按上海日期和任务归属失效。
5. 订单必须包含本地日期、completed、verified 和证据才能进入“昨天的订单”；本实验用测试订单，不生成真实交易。
6. 普通执行没有自动压缩；手动压缩单独测量。摘要调用有实际耗时，不应放到每次唤醒前。

评估时列出的迁移要求包括生产 TaskManager 与 Pi 的生命周期同步、重连与取消一致性、全局手机资源所有权、
长期记忆的查询/编辑/删除接口、持久目录迁移和设备实测。当前实现与剩余验收以 [开发计划](../../docs/plan.md)为准；本目录不会自动启用生产变更。

参考：[官方 Durable README](https://github.com/earendil-works/pi/blob/main/packages/durable/README.md)、
[官方模型接入说明](https://github.com/earendil-works/pi/blob/main/packages/ai/README.md)。
