# jms-mcp

面向 **JumpServer 3.10.15** 的 MCP 网关：读取实例自身的 Swagger 2.0 文档，将选定 API 注册为 MCP 工具。兼容 OpenAPI 3.x 的常用 JSON 接口，提供 Streamable HTTP `/mcp` 和 SSE `/sse`。

当前版本：`0.1.0`。兼容结论基于 JumpServer `v3.10.15` 官方源码、合成接口样本和模拟上游测试；**尚未在真实 JumpServer 3.10.15 实例验收**，不能据此承诺全部 API 可用。上线前请执行 [实例验收清单](docs/VALIDATION.md)。

## 改进内容

| 原设计中的问题 | 本项目处理 |
| --- | --- |
| Swagger 2 请求体未正确变成工具参数 | 原生读取 `in: body`、`definitions` 和本地 `$ref` |
| `basePath` 丢失或 `/api/v1` 重复 | 统一拼接路径，兼容完整路径与部署前缀 |
| 裸 Token 获取文档成功、实际调用失败 | 获取文档与业务请求共用同一认证头 |
| 客户端密钥与 JumpServer Token 混用 | `api_key` 负责 MCP 入站，`api_token` 负责上游 |
| 全量注册导致模型工具超限 | 默认仅资产 GET，白名单、排除列表、最多 100 个工具 |
| 默认关闭 TLS 验证 | 默认校验证书，支持自定义 CA |
| 会话 ID 绕过入站认证 | SSE 消息与 HTTP 每次请求均校验入站密钥 |

## 快速启动

需要 Python 3.11+；推荐 Python 3.12。以下命令在仓库根目录执行。

```bash
git clone https://github.com/abchw517/jms-mcp.git
cd jms-mcp
cp .env.example .env
chmod 600 .env
# 编辑 .env：填写 jumpserver_url、api_token、api_key。
docker compose up -d --build
docker compose logs --tail=100 jms-mcp
curl --fail http://127.0.0.1:8099/readyz
```

默认 Compose 仅映射到本机 `127.0.0.1:8099`。跨主机使用时，配置端口映射、`allowed_hosts`，并通过可信网络或 TLS 反向代理接入。

也可本地安装：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install .
jms-mcp serve
```

## 认证配置

```ini
jumpserver_url=https://jumpserver.example.com
api_token="Bearer <JumpServer访问Token>"
api_key=<独立生成的MCP入站密钥>
```

- `jumpserver_url` 填主机根地址，不能附加 `/api/v1`。部署在子路径时可填 `https://example.com/jms`。
- JumpServer 3.10.15 登录访问令牌使用 `Bearer <token>`；私有 Token 使用 `Token <token>`。Access Key ID/Secret 的 HTTP 签名认证不在本版范围内。
- 裸 `api_token` 默认添加 `Bearer `；裸私有 Token 需设置 `auth_scheme=Token`。不要重复添加认证前缀。
- `api_key` 只填密钥本身。生成示例：`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`。
- Token 过期后更新配置并重启服务；本版不保存密码，也不自动刷新令牌。
- 配置 `jms_org_id` 可为文档请求与业务请求统一发送 `X-JMS-ORG`；不允许工具参数覆盖上游认证。

MCP 客户端示例（Claude Code 等支持 SSE 的客户端）：

```json
{
  "mcpServers": {
    "jms-mcp": {
      "type": "sse",
      "url": "http://127.0.0.1:8099/sse",
      "headers": {"Authorization": "Bearer <MCP入站密钥>"}
    }
  }
}
```

支持 Streamable HTTP 的客户端可使用 `http://127.0.0.1:8099/mcp`，认证头不变；具体配置语法以客户端为准。两种传输均要求客户端在后续消息请求中携带认证头。

## 工具范围与数量

默认配置：

```ini
read_only=true
include_paths=/api/v1/assets/*
max_tools=100
```

过滤条件取交集：GET 限制 → 路径 glob → 精确 operationId 白名单 → operationId 排除 → 标签。`include_operations` 使用文档中的原始 `operationId`；工具名中的特殊字符会转成下划线。重名直接报错。白名单条目不支持正则或通配符。

路径过滤针对合并后的完整 API 路径。如果 Swagger 的 `basePath` 包含部署前缀（如 `/jms/api/v1`），相应设置 `include_paths=/jms/api/v1/assets/*`。

默认只读仅限制 HTTP 方法，不替代 JumpServer RBAC；上游应使用具备所需最小权限的专用账号。若资产接口较多而超过上限，服务启动失败并提示收窄白名单，不会静默截断。该上限只约束本服务，模型对所有 MCP 服务的总工具数量仍需统一控制。

先检查实际工具目录：

```bash
# 读取实例文档，不执行任何业务写操作
jms-mcp catalog --output catalog.json
# 离线分析已下载的 Swagger，无需真实凭证
jms-mcp catalog --schema /tmp/jms-swagger-check.json --output catalog.json
```

输出包含工具总数、过滤数量、跳过接口及原因、每个工具的输入 schema。`catalog.json` 可能含资产接口信息，不要提交到公开仓库。

启用写操作需要同时配置 `read_only=false` 和精确白名单，例如：

```ini
read_only=false
include_operations=assets_hosts_list,assets_hosts_create
```

这个示例 operationId 来自测试样本，实际值以实例文档为准。写操作没有服务端人工确认步骤，也不重试；先在测试组织验收，超时后先核对执行结果再决定是否重发。

工具参数按位置分组，避免路径、查询与请求体同名覆盖：

```json
{"query": {"search": "web", "limit": 20, "offset": 0}}
```

```json
{"path": {"id": "<资产UUID>"}}
```

```json
{"body": {"name": "test-host", "address": "10.0.0.10"}}
```

分页保持 JumpServer 原始语义，不自动拉取全部分页。

## 部署与运行边界

- Docker 使用固定版本 uv，从 `uv.lock` 执行 `uv sync --locked`；锁文件与项目不一致时构建失败。先安装依赖、再安装项目，并使用 BuildKit 缓存加速重复构建。需要启用 BuildKit 的 Docker/Buildx。
- 构建时使用系统 Python 并禁用 uv 下载解释器；运行镜像仅复制 `/opt/venv`，使用非 editable 安装，无需源码目录、uv 或启动时联网安装。`requirements.lock` 保留为兼容导出，Docker 不再使用它。可通过 `--build-arg UV_VERSION=...`、`--build-arg PYTHON_IMAGE=...` 覆盖构建版本，变更后需重新验证。
- 镜像以 UID/GID `10001` 运行，支持只读根文件系统。Kubernetes 模板见 [deploy/kubernetes.yaml](deploy/kubernetes.yaml)。
- 默认校验 TLS。内网 CA 使用 `ca_bundle` 并挂载 CA 文件；`tls_verify=false` 仅用于临时排障。
- 默认关闭环境 HTTP 代理继承，需要时设置 `trust_env=true`。
- `allowed_hosts` 是 MCP 入站 Host 白名单，非 JumpServer 地址。默认只允许 localhost。通过域名/IP 访问时设置，例如 `allowed_hosts=mcp.example.com:*,127.0.0.1:*,localhost:*`。
- SDK 对携带 Origin 的请求校验 `allowed_origins`。需要浏览器来源时显式配置，不提供通配 CORS。
- `/healthz` 检查进程，`/readyz` 检查启动时文档和工具初始化；就绪状态不持续验证 JumpServer 连通性或 Token 有效性。
- SSE 会话存储在进程内；先使用单副本。重启后客户端重连。无状态 `/mcp` 可作为后续多副本部署的基础。
- 默认请求总超时 30 秒、并发 8、响应上限 1 MiB、Swagger 上限 16 MiB。超限返回 MCP 错误，不返回损坏的截断 JSON。
- 不自动重试，不跟随上游重定向，不从 Swagger 的 `host`/`servers` 主机名切换请求目标，不拉取远程 `$ref`。
- 本版不支持文件上传、表单、Cookie 参数、递归请求 schema、复杂 query 序列化。跳过的接口可在 `catalog` 中查看；不会发布缺少必需输入字段的工具。
- API 响应属于不可信外部内容；MCP 调用权限仍由客户端和 JumpServer RBAC 共同管理。

## 开发与验证

```bash
uv sync --frozen --extra dev
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest -q
uv build
```

测试涵盖 Swagger 2 请求体、类型和路径、认证一致性、组织头、过滤与数量上限、错误和响应边界，以及真实 MCP SDK 对两种传输的初始化、工具发现和调用。测试上游为模拟 HTTP，不访问生产 JumpServer。

更多说明：[架构](docs/ARCHITECTURE.md) · [实例验收](docs/VALIDATION.md) · [迁移与故障排查](docs/MIGRATION.md) · [迭代计划](docs/ROADMAP.md) · [变更记录](CHANGELOG.md)。

## 参考来源

设计参考 [aminglinux/jms_mcp](https://github.com/aminglinux/jms_mcp)（分析基线 `eeaea45b88e7ec6ece196df6309581e2f5166ad2`）与 [jumpserver/mcp](https://github.com/jumpserver/mcp)。本项目重新实现协议适配与调用层，未复制这两个仓库的源码。来源与版本依据见 [NOTICE.md](NOTICE.md)。本仓库新实现采用 MIT 许可证。
