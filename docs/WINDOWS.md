# Windows 安装与接入

推荐先使用 [Server URL + OAuth](SERVER-URL.md)，不要求 OpenAI Tunnel ID。已经有专用 Tunnel 权限的用户也可选择下方可选路线。两条路线都复用同一个微信 worker 和私有状态，不同时启动。

## 安装固定依赖

在最终源码目录打开 PowerShell 7；不要搬移已经创建的虚拟环境。

```powershell
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r .\requirements.lock
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
```

当前本机测试为 Windows AMD64 / Python 3.13.12。最低 Python 3.10 的整套结果应查看对应 CI，不能从其他解释器的成功推断。

## 下载并校验官方客户端

仓库不带二进制。固定 OpenAI `tunnel-client v0.0.19` Windows AMD64 包，来源为 [官方 Release](https://github.com/openai/tunnel-client/releases/tag/v0.0.19)。发布包包含 Server URL 所需的 cloudflared 和可选 Tunnel 客户端。只信任仓库中的公开固定摘要；不得下载失败后放弃校验。

以下命令用于首次安装；已有 bin 时先验证原文件，不覆盖运行中的客户端。

```powershell
$release = Get-Content -LiteralPath .\releases\windows-client-v0.0.19.json -Raw -Encoding UTF8 | ConvertFrom-Json
if (Test-Path -LiteralPath .\bin) { throw 'bin exists: verify the existing bundle before replacing it' }
New-Item -ItemType Directory -Path .\bin | Out-Null
Invoke-WebRequest -Uri $release.url -OutFile .\bin\client.zip
if ((Get-FileHash -LiteralPath .\bin\client.zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $release.sha256) {
    throw 'Official archive digest mismatch; do not run it'
}
Expand-Archive -LiteralPath .\bin\client.zip -DestinationPath .\bin\extracted
foreach ($name in @('tunnel-client.exe', 'cloudflared.exe')) {
    $files = @(Get-ChildItem -LiteralPath .\bin\extracted -Recurse -File | Where-Object Name -eq $name)
    if ($files.Count -ne 1) { throw "Unexpected file count: $name" }
    if ((Get-FileHash -LiteralPath $files[0].FullName -Algorithm SHA256).Hash.ToLowerInvariant() -ne $release.files.$name) {
        throw "Official file digest mismatch: $name"
    }
    Copy-Item -LiteralPath $files[0].FullName -Destination (Join-Path .\bin $name)
}
Copy-Item -LiteralPath .\releases\windows-client-v0.0.19.json -Destination .\bin\release-verification.json
& .\.venv\Scripts\python.exe -c 'from pathlib import Path; from probe.client_integrity import verify_bundle; verify_bundle(Path.cwd()); print("bundle PASS")'
if ($LASTEXITCODE -ne 0) { throw 'Bundle verification failed' }
```

ZIP SHA256：`1d0ecc3a54f2a51e27a635ac155f40fbb95df58c52cdeddf6c75532cc0bbeb25`。启动器每次重新验证两个 exe 的文件摘要。临时解包文件位于 Git 忽略的 bin 内。

### 已有部署的升级与回退

v0.0.19 为首次控制面 DNS 失败增加有限、脱敏的诊断，默认可恢复意外退出的 stdio 子进程。后者会重置 MCP 会话；在途请求可能失败，客户端不会重放请求。已有外部进程恢复机制的部署，可按[上游回退指南](https://github.com/openai/tunnel-client/blob/v0.0.19/docs/configuration.md#stdio-failure-policy-upgrade-and-rollback) 在传给客户端的进程环境中使用 `MCP_STDIO_FAILURE_POLICY=exit`，保留整客户端退出语义。不要修改全局环境，也不要在混用旧版本时写入旧版本不支持的新 CLI/YAML 字段。

先在隔离目录验证新源码和完整 bundle，实际切换前一致备份、停止明确归属的旧实例并确认 worker 锁释放，再启动一个新实例。复用原微信状态和原连接，验证认证调用、唯一订阅与真实收发；离线测试不替代这些检查。部署仍需对应授权，本仓库更新不会自动替换运行实例。

回退时恢复匹配的旧源码、旧 bundle 和验证记录。保留的 v0.0.15 manifest 仅用于核验原归档，当前启动器会拒绝混用旧发布记录。不要只替换 manifest、并行启动两个客户端，或用旧数据库覆盖新消息。诊断改进不等于网络故障根因已经修复。

## 离线验证

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s probe_tests
if ($LASTEXITCODE -ne 0) { throw 'Unit tests failed' }
& .\.venv\Scripts\python.exe .\scripts\generate-schema.py --check
if ($LASTEXITCODE -ne 0) { throw 'Schema check failed' }
```

Windows 私有 ACL、重解析点拒绝、跨进程锁、Job 资源限制和客户端本机 fixture 都要实际通过。[测试边界](TESTING.md) 说明何时可以报告 PASS。

## Server URL + OAuth

安装和测试通过后运行：

```powershell
& .\.venv\Scripts\python.exe .\scripts\run-server-url.py
```

首次未知 callback 主机时保持默认拒绝全部。按 [SERVER-URL.md](SERVER-URL.md) 创建仅本人可用的自定义 MCP、本人在本机批准 OAuth、验证真实工具调用，再扫码或复用原微信绑定。精确 callback 主机确认后才配置允许列表。网关不会打印 token。

前台进程须保持运行。Quick Tunnel 地址临时有效，重启需要更新连接并重新授权；当前代码没有自动恢复部署或持久 OAuth。不要把手动可恢复宣称为无人值守通过。

## 可选：Secure MCP Tunnel

只有账号实际提供 Tunnel 与相关权限时才使用。[官方指南](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) 描述组织、工作区和运行密钥范围。本人创建专用私有 Tunnel、私下输入运行密钥，然后：

```powershell
& .\scripts\setup-windows.ps1
& .\bin\tunnel-client.exe doctor --profile-file .\runtime\profile.yaml --explain
& .\.venv\Scripts\python.exe .\scripts\run-windows.py
```

运行进程不使用 Admin key。Tunnel 访问控制须仅本人可用；有 Tunnel 配置不代表插件注册、事件执行或手机收件已完成。

## Windows 实现与停止

| 上游机制 | Windows 实现 |
|---|---|
| fcntl 单实例锁 | msvcrt 跨进程非阻塞锁 |
| POSIX 私有权限 | 当前用户 / SYSTEM 的受保护 DACL，拒绝重解析点和 ADS |
| systemd 前台管理 | Python 启动器与 Windows Job Object |
| Unix 路径与资源计数 | Windows Python 路径、UTF-8、进程内存计数 |
| 微信与 MCP Events | 保留协议、持久队列、回复与媒体契约 |

Job 限制一个可用逻辑处理器、合计提交内存384MiB、活动进程64，启动器退出时清理其后代。该进程数限制不是 Linux 的线程 TasksMax。峰值计数可能包含失败的超限申请，OOM 计数不可识别时输出 null。

Ctrl-C 停止自己的实例并保留 runtime；不安装 Windows 服务、自启动或第二个调度器。更新先测试隔离候选，复用原状态，不能用旧数据库覆盖新来信。私有 ACL 不防御同一用户进程或可接管的管理员。
