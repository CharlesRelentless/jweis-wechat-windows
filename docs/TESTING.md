# 测试与验收

在源码根目录安装 requirements.lock 和经过完整性校验的官方 Windows 客户端后运行：

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s probe_tests
& .\.venv\Scripts\python.exe .\scripts\generate-schema.py --check
```

测试使用合成状态、临时数据库和本机协议 fixture，不绑定微信，不使用真实账号。Windows 客户端集成测试需要 bin 中的官方客户端；缺少文件时会 skip，所以提交验收应记录 skip 的原因。Linux 测试和 Windows 测试分别报告，不能互相代替。

| 层次 | 证据 | 不能替代的检查 |
|---|---|---|
| 离线业务与安全边界 | unit tests、schema、Windows ACL / 锁 / Job | 产品账号权限、手机收件 |
| 连接 | health/ready、匿名 MCP401、真实认证工具调用 | 微信原文和回复 |
| 原生本机任务 | 原 dot 执行的路径、结果、退出码 | 本地文件到微信完整往返 |
| 端到端 | 真实微信请求、本机读结果、稳定 ID、发送状态、本人收件 | 长时间静默、续期和重启恢复 |
| 无人值守 | 被动采样、同一订阅实际续期、资源趋势、恢复验收 | 单次小样本成功 |

当前真实本机读取修复及恢复后的完整往返已在一次本人部署中通过。历史前台运行测试在应用重启后失败，未完成48小时；新连接不得计入旧测试并标为通过。

公开记录只保存命令类型、时间范围、结果和脱敏证据，不保存用户账号、原微信正文、私有任务 ID 或 callback URL。真实部署和验收记录留在自己的私有目录。

## 运行状态诊断回归

`probe_tests/test_safe_status.py` 覆盖当前 Server URL 端口优先、损坏/不可读 endpoint 不回退、旧 Tunnel 兼容、非本机和带凭据地址拒绝、停止后的陈旧 `ready`、时钟超前、订阅到期/重复、网关不可达、未绑定状态，以及只读和隐私边界。全部使用合成数据库和探测替身，不读取真实消息或凭据。

## 2026-10-11 已发布源码验证

- Windows AMD64 / Python 3.13.12：132 tests，131通过、1个 Linux 平台测试跳过，16.415秒；schema 检查通过。
- 固定官方客户端摘要验证通过；真实客户端在隔离的本机合成控制面完成事件发现和工具转发。
- schema 检查通过；2MiB文件与小PNG合成出站资源检查通过，没有网络请求或真实微信发送。
- 修复离线集成 fixture 对源码目录 `.venv` 的假定：使用调用方解释器新建临时环境，兼容干净源码和 CI。该修复未部署到真实桥接。
- [GitHub Actions 实测](https://github.com/CharlesRelentless/jweis-wechat-windows/actions/runs/38119920815)：源码提交 `d4dcdcbf970b262c494fd5480b3e48c6da43a3dc` 的 Windows / Python 3.13、Linux / Python 3.10 和 3.14 三个作业全部通过；包含全套测试、schema 和合成出站资源检查。Windows 作业另验证官方客户端及当前运行身份的私有 ACL。
- Windows runner 的默认短路径临时目录曾触发路径安全检查，现改用明确的标准临时目录。内置管理员身份的 SDDL 表示曾触发私有 ACL 判定失败，现从当前进程身份生成可信的规范表示；管理员组权限仍被拒绝，并增加真实 Windows ACL 反例测试。上述失败运行保留在 Actions 历史中。
- 官方客户端离线协议 fixture 曾有一次请求超时；独立检查、随后全套本机测试和上述 CI 均通过。协议测试通过不代表网络服务具有可用性保证。
- [后续重复 CI](https://github.com/CharlesRelentless/jweis-wechat-windows/actions/runs/38120263584) 的 Windows 作业曾报 JSON 解析失败，其余业务测试与 Linux 作业通过。离线 fixture 现等待完整的本机连接信息，而不只检查文件存在；增加部分写入、发布超时和非本机地址三个回归检查，并为 JSON 错误标明执行阶段。该失败记录保留，未将此前一次通过等同于长期稳定。
- 真实微信事件只投递一次，原 dot 镜像准确原文；同一个原生本机任务重新执行限定文件读取，四项结果与 final 一致。ack 与 final 各发送一次、各被 API 接受；本人手机确认 final 只有一条。
- 完整往返 PASS 只覆盖这一次无敏感小样本。临时地址、内存 OAuth 与前台进程重启恢复仍未通过无人值守验收。
