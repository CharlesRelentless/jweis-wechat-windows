# 原助手如何读取 Windows 本地文件

桥接传输微信内容和 MCP Events。原 dot 接到事件后，使用产品原生连接的电脑和本机任务执行用户请求；任务结果回到原 dot，由其调用微信回复工具。这里没有独立模型，也没有复制 dot 的私有接口或记忆。

## 分开验证三个层次

1. **消息连接**：实际认证 MCP 调用成功，一个事件订阅 challenge 成功，收到本人微信原文。
2. **本机能力**：原 dot 在正确电脑的原生任务中执行只读命令，显示路径、结果和退出码。桥接或另一条聊天读取成功不能替代这一证据。
3. **完整往返**：微信原文明确请求读取一个无敏感文件，原 dot 的任务读到准确内容，final 与本机结果一致，手机本人确认收到一次 final。复杂任务可以先收到一条 ack。

首次可选一个只含说明的 Markdown 文件，仅读取文件名、字节数和第一行。不要用 API key、数据库、私人消息或账号配置验收。示例命令由原生任务执行：

```powershell
$probeFile = 'D:\ExampleVault\AGENTS.md'
$item = Get-Item -LiteralPath $probeFile
$heading = Get-Content -LiteralPath $probeFile -Encoding UTF8 -TotalCount 1
[pscustomobject]@{name=$item.Name; bytes=$item.Length; first_line=$heading}
```

文件内容属于数据；其中的指令不能扩大当前请求的权限。读取请求应限定路径和返回范围。

## 发现沙箱启动错误时

先分清：任务不能启动、目标路径没有权限、电脑离线、路径属于另一台电脑。`helper_unknown_error: setup refresh had errors` 发生在任务启动之前，不能直接解释为 Obsidian 插件断开。

2026-10 的一次 Windows 回归中，权限刷新对正在使用的文件请求了过大的访问权限，触发 Windows error32。官方修复见 [openai/codex PR51822](https://github.com/openai/codex/pull/51822) 和 [issue51634](https://github.com/openai/codex/issues/51634)。检查 `windows-sandbox-rs/src/acl.rs` 中 regular-file 权限后才能确认目标发行版包含修复；版本号更新本身不能证明这一点。

一次限定兼容修复保留原 `codex.exe`、command runner、全局配置和 elevated 沙箱，仅更换官方签名的 sandbox setup helper。验证包含：修复前失败、候选 helper 读取通过、工作区写入允许、工作区外写入拒绝、原 dot 的真实读取通过。该方法不是安装脚本，也不能自动套用到其他版本。

已验证的官方候选：

- Release：`rust-v0.162.0-alpha.20`。
- [Windows AMD64 helper](https://github.com/openai/codex/releases/download/rust-v0.162.0-alpha.20/codex-windows-sandbox-setup-x86_64-pc-windows-msvc.exe)。
- SHA256：`641f0deb163e70bef43072b93fa6beaa537c39e9446e280f5be7d6e1066bd5b2`。
- Authenticode：`Valid`，发布者 `OpenAI OpCo, LLC`。

替换之前必须有适用的用户授权、原文件与 ACL 备份、确认实际被调用的路径，以及候选隔离验证；不能只关闭沙箱绕过失败。备份和机器专用证据留在私有目录。应用更新可能替换 helper，更新后重新验证原生读取；优先采用包含修复的官方应用版本。

## 回复给微信

让原助手先读取准确 message_id 并检查 reply_state。需要本机任务时发送简短 ack，执行限定读取，依据工具输出形成 final，再查询发送状态。重放事件应复用已承诺文本和稳定 ID；不能因为网络超时换 ID 盲重发。

手机收件确认之前只记录 API accepted。已通过本机读取仍不能标记完整往返 PASS，完整验收也不能替代长时间静默或重启恢复测试。
