# 迁移与故障排查

## 从 aminglinux/jms_mcp 迁移

保留 `jumpserver_url`、`api_token`、`api_key`、`server_port`、`base_path` 等环境变量名称。差异如下：

- 不支持旧的 `api_base_url` 手工补前缀方式；路径由 Swagger 自动解析。迁移时删除此配置。
- 默认启用 TLS 校验；内网证书需提供 CA，不能沿用原项目默认忽略证书的假设。
- 客户端固定使用 MCP `api_key`，服务端使用 JumpServer `api_token`；客户端令牌不会透传给上游。
- 默认只开放资产 GET；需要其他模块时修改 `include_paths`。需要写操作时增加精确 operationId 白名单。
- 工具入参改为 `path`、`query`、`body` 分组；客户端需重新发现工具目录。
- SSE 后续消息也必须带 Authorization；只在首次 SSE GET 携带密钥的客户端需要升级或改用支持完整认证的传输。

## 常见现象

| 现象 | 排查 |
| --- | --- |
| `/sse` 或 `/mcp` 401 | 检查入站 `api_key`，客户端头为 `Bearer <api_key>`；不是 JumpServer Token |
| 获取 Swagger 401 | 检查 `api_token` 类型、前缀、有效期；私有 Token 使用 `Token` |
| Swagger 200，但工具调用 401 | 本项目共用认证头；检查 Token 是否过期、是否运行了旧镜像 |
| 工具调用 403 | 核对专用账号权限与 `jms_org_id`；连接 MCP 成功不意味着上游授权充分 |
| HTTP 421 / Host 验证失败 | 把实际访问域名/IP及端口加入 `allowed_hosts`，不要填 JumpServer 域名 |
| Origin 验证失败 | 在 `allowed_origins` 中加入客户端显式发送的 Origin |
| 没有工具 / 超过 max_tools | 离线查看 Swagger，收窄 `include_paths` 或精确 `include_operations` |
| 部分接口 skipped | 查看 catalog 原因；表单、文件、递归请求等当前不支持 |
| API 404 | 核对 Swagger `basePath`、样本路径、根地址及反向代理路径前缀 |
| TLS 错误 | 挂载内部 CA，设置 `ca_bundle`；同时检查网关到 JumpServer 的网络 |
| 请求超时 | 核对上游、代理、网络与并发；写操作先核实执行结果，不要盲目重发 |
| 响应超限 | 减小 limit / 缩小查询范围；确有必要再提高 max_response_bytes |

Kubernetes：先创建命名空间和 Secret，再替换镜像及 ConfigMap 地址，应用模板。

```bash
kubectl create namespace jms-mcp
# 建议通过现有密钥管理流程创建 jms-mcp-auth。
# 如需从本机文件导入：文件只含 api_key=... 和 api_token=...，且权限为0600。
kubectl -n jms-mcp create secret generic jms-mcp-auth --from-env-file=./auth.env
kubectl apply -f deploy/kubernetes.yaml
kubectl -n jms-mcp rollout status deployment/jms-mcp --timeout=180s
```

通过 Ingress/Gateway 暴露时，补齐 Host/Origin 白名单和 TLS。SSE 反向代理需关闭响应缓冲并允许长连接。
