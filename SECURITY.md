# 安全与隐私

公开源码与私人微信连接是两个独立范围。不要共享运行连接、token、二维码、日志、数据库、批准 challenge 或真实 callback URL。

Server URL 入口要求本人本机批准的 OAuth，使用精确 redirect、issuer、resource 和 S256 PKCE。Tunnel 方案依赖仅本人可访问的专用隧道和插件。微信 worker 只接受绑定者私聊。

回调必须命中已核实的精确 HTTPS 主机，保持正常 TLS 验证与目标地址检查。不得为了连接失败放开主机通配符、关闭证书校验或公开无认证 MCP。

私有 Windows 文件采用当前用户与 SYSTEM 的受保护 DACL，并拒绝重解析点和备用数据流。这一边界不能防御同一用户的其他进程或具备接管权限的管理员。

报告问题时只提供脱敏复现、版本、错误类型和合成 fixture。GitHub issue 与 PR 不应包含任何真实凭据或私人消息。如果发现凭据暴露，先由本人撤销凭据和连接；删除当前文件不代表 Git 历史已清除。
