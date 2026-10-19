# 上游来源与基线

本项目⑦基于项目⑤ **Secure Agent Runtime & Deployment Gateway** 的安全加固版本。

| 项 | 值 |
|---|---|
| 上游仓库 | https://github.com/haozhouliuliuliu/it-agent-collab |
| 上游基线提交 | `fb17bfcc48c861b8e4abc33bd15bf484d94a447a` |
| 上游版本 | `v1.0.0` |
| 上游许可证 | MIT（见 `LICENSE`） |
| 本项目 | https://github.com/haozhouliuliuliu/secure-agent-runtime |

## 保留与修改

保留：

- 四个 Agent 的编排、结构化交接、审核与 SQLite 状态模型；
- 虚构业务数据与制度（`seed_data/`）；
- MIT 许可证与版权声明。

安全加固：

- 新增程序级工具最小权限（模型工具列表 + 执行前授权双强制）；
- 新增 FastAPI 受控 API、API key 认证、任务所有权与管理员权限；
- 新增并发安全预算、单用户限流、全局并发上限、输入长度限制；
- 新增日志脱敏、错误响应脱敏、任务恢复、备份/恢复与 HTTPS 部署配置；
- 新增 80+ 安全与功能测试、CI（含 Docker 与 HTTPS Compose）。

未修改项目⑤的 `main` 或 `v1.0.0`；本仓库为独立仓库。
