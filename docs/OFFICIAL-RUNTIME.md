# 官方隧道的 Windows 原生运行与监控

这是已有专用官方 Tunnel 的可选运行方式。首次账号授权和密钥保存需要本人或其明确委托；源码本身不提供账号权限，也不表示已获准登记 Windows 任务。Server URL 用户仍按原安装指南操作，不同时运行两个 worker。

## 前置条件

- 使用通过摘要验证的完整官方 Windows bundle、已有虚拟环境和同一个私有 state 目录。
- `render-config.py --confirmed-env` 可以生成引用 `env:JWEIS_TUNNEL_API_KEY` 的 profile。密钥由正式安全保存流程写入私有 `runtime/secrets/openai-tunnel.env`；不要粘贴到聊天、脚本参数或公开配置。
- 私有 `runtime/official-tunnel-activation.json` 必须与 profile 的真实 Tunnel ID 一致。仅在确有免费依据时使用 `confirmed_free`；否则需要本人的明确持续运行许可以及 `no_charge_observed` / `continue_until_charge`。`charge_detected=true` 和撤销启动许可始终阻止运行，不能自动解除。
- 私有 `runtime/transport.json` 的 `kind` 为 `official_tunnel`，ID 与上述配置一致。它是运行方式与监控的元数据，不能作为调用者身份证明或授权凭据。
- 需要代理时仅沿用已核验的本机 HTTP 代理，不修改全局环境、DNS 或防火墙。

## 启动和恢复

获得对应主机操作授权后：

```powershell
# 默认准备一个禁用任务；显式 -Activate 才启用。
& .\scripts\install-official-tunnel-runtime.ps1 -Activate
Start-ScheduledTask -TaskName JweisBridgeOfficialTunnel
```

原生任务在当前用户登录后启动，直接托管已安装的 Python 解释器。IgnoreNew、worker 锁和 Windows Job 保留单实例及原资源边界。每分钟的原生触发器可以补启动，连续三次失败后冷却30分钟；连续健康五分钟后清零失败预算。不建立额外服务或调度进程。

同一启动器每15秒检查本机 health/ready 和控制面组件。启动宽限120秒后持续异常180秒才记录证据并停止所属客户端，随后由原生任务补启动。正常长轮询和队列背压不会单独触发重启。收费或本人停机优先，诊断写入失败时不贸然停止客户端。

v0.0.19 还支持客户端内部恢复 stdio worker，丢失的会话需要重新初始化，在途调用不会自动重放。若使用外部整客户端恢复策略，见 [Windows 升级与回退](WINDOWS.md#已有部署的升级与回退)。始终保留原发送身份，不重发未知结果。

## 只读监控

```powershell
& .\scripts\install-runtime-check.ps1 -IntervalMinutes 5
& .\.venv\Scripts\python.exe scripts/safe-status.py --state-dir runtime/state
& .\.venv\Scripts\python.exe scripts/check-runtime.py --public-check
```

初始化会核对当前已授权的官方 Tunnel 并固定其身份。已有的不同 pin 不会被覆盖；跨连接迁移需要单独完成并验证。检查不读取运行密钥，不调用 MCP 工具，不发送微信，也不改变订阅。

官方隧道的 PASS 要同时满足 health/ready、微信轮询新鲜、有效订阅恰好一个、准确 Tunnel ID 和实时控制面正常。特别是 health/ready 为200、微信轮询仍新鲜时，控制面失败也必须返回 FAIL。接近订阅到期单独返回 WARN。匿名健康正常仍不能证明认证工具或手机收件。

## 验证和故障留证

- `control-plane-status.json` 与有界诊断历史仅保存固定组件、状态、计数、时间和 RPC 阶段，不保存正文、参数、结果、回调、账号或密钥。
- 当前消息、数据库、凭据、endpoint、二维码、日志和恢复记录均留在私有 runtime 中。切换前使用 SQLite backup API 做一致快照，回退不以旧数据库覆盖新消息。
- 回归检查包含实际本机 HTTP fixture：模拟控制面连续失败，同时让两个旧健康端点返回200且保留新鲜轮询和唯一订阅；CLI 必须失败。初始化测试验证固定官方身份并拒绝静默迁移。
- health 检查、隔离故障复现、真实认证调用、原 Jweis 的一次回复与本人手机收件分别验收。单次通过不能证明长期稳定。

此方式依赖电脑在线、当前用户已登录以及所需代理和网络可用。关机或休眠期间不能响应微信，唤醒后恢复与连续在线是不同的验收项目。历史失败记录应保留，新的观察单独记录；不修改电脑的电源设置作为隐含步骤。
