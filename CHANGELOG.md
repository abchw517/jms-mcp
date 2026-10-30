# Changelog

## 0.1.0 — 2026-10-30

- 原生解析 Swagger 2 请求体、definitions、本地引用、查询参数和路径级参数。
- 合并 basePath 与接口路径，避免遗漏或重复 /api/v1。
- 统一 Bearer/Token 认证；独立入站密钥；组织头配置。
- 默认资产 GET 白名单，写操作需显式放行；工具数量硬上限。
- SSE 和 Streamable HTTP；每次消息均验证入站密钥。
- TLS 校验、连接复用、并发与响应大小边界、总超时、无自动重试。
- 提供离线 catalog、非 root Docker、Compose、Kubernetes 模板、CI 和兼容测试。

兼容状态：合成接口与模拟上游验证；真实 JumpServer 3.10.15 验收待完成。
