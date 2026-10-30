# 设计与兼容性依据

- 设计参考：<https://github.com/aminglinux/jms_mcp>，分析基线提交 `eeaea45b88e7ec6ece196df6309581e2f5166ad2`。
- 上述仓库的上游：<https://github.com/jumpserver/mcp>。
- JumpServer 版本依据：<https://github.com/jumpserver/jumpserver/tree/v3.10.15>。
- 文档路由：`apps/jumpserver/urls.py`；Swagger 生成：`apps/jumpserver/views/swagger.py`。
- Bearer 与私有 Token 认证依据：`apps/authentication/backends/drf.py`。

本仓库独立实现 Swagger/OpenAPI 到 MCP 工具的编译、HTTP 调用和认证。未复制参考仓库的源文件；MIT 许可证仅适用于本仓库新实现。依赖项保留其各自许可证。

`tests/fixtures/jumpserver-3.10.15-minimal.json` 为根据上述接口形态手工编写的合成样本，不是生产环境 Swagger 导出，不含真实资产或凭证。
