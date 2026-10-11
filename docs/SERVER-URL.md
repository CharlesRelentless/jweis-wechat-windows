# Windows：不用 OpenAI Tunnel ID 的接入方式

使用 ChatGPT 的 **Server URL + OAuth** 连接现有 dot、微信 worker、状态目录、媒体契约和事件协议。不需要 Platform Admin key、OpenAI Tunnel ID 或模型 API key。先按 [Windows 安装指南](WINDOWS.md) 下载校验官方客户端并通过离线测试。

## 当前适用范围

默认模式是前台运行的单人验证连接。Cloudflare Quick Tunnel 产生临时 HTTPS 地址；不需要 Cloudflare 账号或域名，进程停止后地址失效，重启产生新地址。该服务不保证持续可用，长期使用需要固定 HTTPS 域名。默认 OAuth 状态只在本次运行内有效；重启后需要重新授权。另有下方明确启用的固定入口和授权状态持久化模式。微信历史和订阅仍留在原私有 `runtime/state`，不清除原有状态。

桥接使用 JSON 形式的 Streamable HTTP，不依赖 SSE。微信来信经已有 MCP Events 的出站 HTTPS webhook 推送，保留协议 `2026-07-28`。Cloudflare 不提供微信消息的访问权限；真正 MCP 访问控制由 OAuth 网关执行。

## 启动与连接

在安装根目录运行：

```powershell
& .\.venv\Scripts\python.exe .\scripts\run-server-url.py
```

该命令校验已固定的官方客户端发布包、应用 Windows Job Object 的1核/384MiB/64进程限制，启动原 stdio worker 和认证网关，再启动本次进程独有的 Cloudflare 连接。其他实例不被停止，不安装自启动，不改代理或防火墙。原 worker 锁仍防止同一状态目录的第二个 worker。

启动后打印 `SERVER_URL=https://<temporary-host>.trycloudflare.com/mcp`。真实运行地址仅写入私有 `runtime/server-url/endpoint.json`，不写到源码或公开报告。

在电脑 Chrome 的 ChatGPT 插件目录添加自定义 MCP：

- 名称：`Jweis 微信桥接`
- 连接：`服务器 URL`；使用本次实际地址。
- 身份验证：`OAuth`。授权服务器支持 DCR、S256 PKCE、公有客户端 `none`，不启用 CIMD。
- 核对连接范围和风险说明后创建本人私有插件，不分享给其他账号。

此时公网只能读无敏感健康状态及认证元数据；未批准的客户端不能调用任何 MCP 方法，包括工具清单和事件方法。

## 本人批准 OAuth

ChatGPT 授权页面会显示一个请求 ID，并等待电脑主人批准。请本人在本机终端执行：

```powershell
& .\.venv\Scripts\python.exe .\scripts\approve-server-url.py --request '<页面显示的32位请求ID>'
```

核对终端与授权页面的请求 ID、返回 ChatGPT 的精确地址和权限范围。确认后在本机输入 `APPROVE`。授权页面会自动返回 ChatGPT，无需把密钥、密码或授权码发到聊天。

默认只允许官方说明中的 `https://chatgpt.com/connector_platform_oauth_redirect`。网关在每个授权响应中返回精确 `iss`。若实际产品展示其他 redirect URI，先核对官方管理页面，不能放开通配符或其他域名。

本人已明确授权当前操作者代为批准同一连接时，可以对**从本人登录浏览器核对的一个当前请求**加 `--yes`，无需再次键入 APPROVE。脚本仍核对到期时间、精确返回地址、原权限和当前 endpoint 的 origin。该选项属于本机操作，不存在公网自动批准接口；不能遍历批准目录中的未知请求。

## 固定入口与授权恢复

已经有一个固定 HTTPS 反向代理入口时，可明确启用：

```powershell
& .\.venv\Scripts\python.exe .\scripts\run-server-url.py --public-origin 'https://<本人固定域名>' --port 8787 --persistent-oauth
```

此模式只在 `127.0.0.1:8787` 启动网关，不创建 Quick Tunnel。必须先让既有 HTTPS 入口正确转发到此端口；命令不会购买域名、创建第三方账号、部署代理或安装自启动。首次注册须使用该固定 origin。迁移既有临时插件是另外一个部署步骤，不能仅启用该命令就宣称迁移完成。

私有 `runtime/server-url/oauth/authorization-state.json` 只保存客户端元数据及访问/刷新令牌的 SHA256 校验值，不保存原始 bearer、refresh token、授权码或待批准请求。文件保持 owner+SYSTEM 权限，原子替换前先 fsync；单写入者锁阻止多个网关覆盖授权状态。必须在同一 issuer 下恢复，origin 改变、文件损坏或权限不符时失败关闭；不能静默重置后批准公网请求。

已有未到期刷新令牌可在同一 origin 的进程重启后继续兑换，无需再次批准。访问令牌仍1小时到期、刷新令牌仍24小时到期并逐次轮换；新令牌返回前提交轮换状态，旧刷新令牌在重启后仍被拒绝。重启恢复不取消到期、撤销、账号变化或微信手机确认要求；不能把客户端 ID 视为可自动授权的凭据。停止网关后删除该私有状态文件会清除所保存的 grant；运行时不能以删除文件代替撤销。

该模式通过隔离状态和原有 HTTP 边界测试；真实固定域名、产品自动刷新、进程启动及微信长时间收发仍需部署后分别验收。当前临时部署不会因源码更新而自动转换。

批准由私有目录中的一次性随机 challenge 绑定；公开 OAuth 请求本身不能批准。授权码90秒到期且只能兑换一次，绑定客户端、返回地址、资源与 S256 PKCE。访问令牌1小时到期；刷新令牌24小时到期并逐次轮换。令牌不输出到日志或项目报告，操作系统私有文件权限保持原边界。

## 完成真实接通

1. 安装插件后，在**已有 Jweis** 实际调用 `get_weixin_status`；这一步通过才开始本人微信绑定。
2. 本人扫描腾讯二维码并手机确认；沿用 `probe/login.py`，无需 OpenAI 运行密钥。
3. 让已有 Jweis 为 `weixin.message` 尝试一个订阅。默认拒绝所有未知 callback；通过 `get_probe_status` 核对实际精确主机后，在本人授权下重启前台启动器，加 `--allow-callback-host '<已核实的精确主机>'`。重启后的临时地址和 OAuth 授权需要更新，原微信状态不变。先核对账号是否能更新原连接地址：本次管理界面没有 URL 编辑，原插件版本上传也拒绝更换 canonical app，因此临时地址改变后需为同一 Jweis 重新登记连接入口，不能把插件压缩包更新当作地址迁移成功。先验证新的认证工具，再尝试订阅；旧入口随原进程停止失效，不自动删除用户配置。长期运行应在登记前确定固定 HTTPS 地址，避免反复登记和批准。
4. 按 `docs/REPLY-WORKFLOW.md` 验收本人文字往返和无重复。只有实际收到来信、Jweis 工具调用和本人手机收件确认才算接通。
5. 在具体授权后按 `docs/MEDIA.md` 验收合成 PNG/TXT 双向字节和 SHA256。

图片附件进入 AI 产品后可能被转换。发送前仍须核对实际取得的文件大小和 whole SHA256；不一致时先停止该图片发送。可通过普通文件附件传递原图字节，或用文本文件封装 base64，让调用方严格解码、重新核对原图哈希后，以 `kind=image` 上传实际图片。封装文件只用于传输原字节，不额外发送给微信；原稳定上传和发送 ID 保持不变。

桥接返回有限 `refreshBefore`，并在到期后停止事件投递。官方 MCP Events 文档说明 ChatGPT 在到期前以相同订阅身份原生刷新；这不需要另建轮询器或调度器。账号的实际续期、24小时闲置和电脑重启仍应分别验证，不能以文档替代观察。

仅运行服务、取得 URL、健康返回200、匿名访问返回401或离线测试通过，都不是上述端到端完成证据。

## 本机回调网络路径

若实际 callback challenge 超时，先分别核验精确回调主机的直连和电脑现有代理路径。此 worker 的原始 HTTP 客户端不会自动使用 Windows 系统代理；网页能访问不能证明回调可达。核验已有 loopback HTTP CONNECT 代理可正常进行 TLS 后，可以只给这个桥接配置：

```powershell
& .\.venv\Scripts\python.exe .\scripts\configure-callback-network.py --proxy-url 'http://127.0.0.1:<已核验端口>'
```

代理地址只能是显式 loopback IP，不允许凭据、路径或远端代理。配置保存在私有 `runtime/state/callback-network.json`，不修改系统代理、DNS、hosts 或其他应用。装有此修复的 worker 每次回调重新读取该文件，之后修改回调代理不必重启；旧代码首次升级仍须重启，并处理临时地址与 OAuth 的变化。`--direct` 恢复原直连。

回调仍先检查精确 HTTPS 主机白名单。代理模式通过固定的 Cloudflare DoH HTTPS 服务解析 A 记录，校验准确查询、响应大小及所有目标公网 IPv4；代理 CONNECT 的目标是校验过的数字 IP，回调 TLS/SNI 仍验证原主机与正常证书。禁止跟随回调或 DNS 服务重定向，不盲目重试 POST。微信和附件网络路径不因此改变。固定解析服务不可用、配置损坏或发现非公网地址时失败关闭，不能自动放宽或回退到未验证路径。

解析协议依据：[Cloudflare JSON DoH](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/)。该格式没有独立 RFC，解析边界按当前服务响应验证；公网连接和持续事件仍需实际验收。

## 停止与回退

在本次启动终端按 Ctrl-C；启动器只结束自己创建的 Cloudflare 进程和 worker，保留私有微信状态。没有自启动或全局账号配置可回退。源 Tunnel 方案文件仍保留以便比较，当前任务不要求继续取得 Tunnel ID。

## 官方依据

- [ChatGPT 自定义 MCP：Server URL 和 OAuth](https://developers.openai.com/api/docs/guides/custom-mcp-server)
- [OpenAI MCP OAuth：资源、S256、DCR、精确 issuer 和 redirect](https://developers.openai.com/plugins/build/auth)
- [MCP Events：同一认证接口与 webhook](https://developers.openai.com/plugins/build/mcp-events)
- [Cloudflare Quick Tunnels：无需账号、临时地址、限制](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
