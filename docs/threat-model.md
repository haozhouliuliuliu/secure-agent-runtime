# 威胁模型

> 范围：单节点、内部服务、虚构企业 IT 数据、只读分析型 Agent。
> 不覆盖：真实企业系统接入、Kubernetes、多租户 SaaS、生产级 PKI。

## 1. 资产

| 资产 | 说明 |
|---|---|
| 模型 API 密钥 | DeepSeek / Qwen / Kimi，可用于产生费用与访问模型 |
| 服务 API key | 调用方身份凭证，绑定 user 与 role |
| 任务数据 | 用户请求、Agent 结论、证据、最终报告 |
| 业务数据 | 虚构库存/设备/工单/制度，只读 |
| 审计记录 | 模型调用、工具调用、拒绝事件、错误码 |
| 数据库文件 | SQLite，包含任务与轨迹 |

## 2. 信任边界

```text
客户端 ──HTTPS──> Caddy ──HTTP(内网)──> Agent API
                                          ├─ 认证/授权
                                          ├─ 任务服务（并发/限流）
                                          ├─ Orchestrator（Agent）
                                          ├─ 只读工具（按 Agent 白名单）
                                          ├─ 模型供应商（外部，HTTPS）
                                          └─ SQLite 卷
```

- 客户端输入、工单描述、制度文本：**不可信数据**；
- 模型输出：不可信，必须经结构化校验与程序规则；
- 模型供应商响应：不可信，错误细节不向客户端透传；
- 容器内进程：非 root；密钥只来自环境变量。

## 3. 威胁与控制

| # | 威胁 | 影响 | 控制 | 验证 |
|---|---|---|---|---|
| T1 | Business 调用制度检索 / Policy 调用库存与工单 | 越权读取、角色边界失效 | 两层授权：`schemas_for(agent)` 只发允许工具；`execute(agent, ...)` 执行前再校验 | `test_tools.py`、`test_security.py` |
| T2 | Auditor/Planner 执行工具 | 审核者污染证据 | 两者工具列表为空，执行层拒绝；审核代码不处理 tool_calls | `test_planner_and_auditor_have_no_tools`、`test_auditor_cannot_execute_any_tool` |
| T3 | 模型要求不存在/越权工具 | 数据泄漏或命令执行 | 工具白名单 + 无 SQL/文件/命令工具；拒绝事件记录，不返回数据 | `test_business_unknown_tool_is_denied_without_data` |
| T4 | Policy 不可用时 Business 代替检索制度 | 伪造成制度结论 | Business 无 `search_policies` 权限；最终报告 `policy_unavailable=true`，policy 字段为空 | `test_policy_model_failure_does_not_trigger_substitute_search` |
| T5 | 未认证访问任务接口 | 数据泄漏 | 所有 `/v1/tasks*` 需要 Bearer key；缺失/错误 401 | `test_missing_and_invalid_token_return_401` |
| T6 | 普通用户访问管理员接口 | 权限提升 | `require_admin` 403 | `test_admin_can_read_any_task_and_executor_gets_403_on_admin_api` |
| T7 | 跨用户读取任务 | 数据泄漏 | task.owner 校验；非本人返回 404（与不存在一致） | `test_cross_user_access_returns_404` |
| T8 | 任务列表泄漏他人任务 | 数据泄漏 | 非管理员按 owner 过滤；管理员单独端点 | `test_task_list_is_owner_scoped` |
| T9 | 提示词注入（工单/制度中的“忽略规则”） | 越权/伪造成功 | 文本只作为数据；工具授权在程序层；注入正则标记 + 审核 finding | `test_ticket_injection_is_returned_as_data`、`test_untrusted_ticket_instruction_is_flagged` |
| T10 | 模型 401/429/5xx/超时/无效 JSON/不存在模型 | 伪成功或无限重试 | 错误码分类、有限重试、失败落库、任务降级；客户端只看到稳定错误码 | `test_llm.py`、`test_model_error_is_sanitized`、`test_invalid_model_json_fails_without_fake_success` |
| T11 | 并发绕过全局/单任务预算 | 成本失控 | `BudgetManager` 全局锁 + 每任务独立计数；预留后才请求 | `test_global_budget_is_concurrency_safe`、`test_per_task_model_budget_is_enforced` |
| T12 | 请求洪泛/过载 | 资源耗尽 | 单用户滑动窗口限流 429；全局并发信号量 503；输入长度 413 | `test_rate_limit_returns_429`、`test_overload_returns_503`、`test_oversized_input_is_rejected` |
| T13 | 密钥进入日志/错误响应 | 凭证泄漏 | 日志 Formatter 脱敏；不记录 Authorization；API 只返回错误码 | `test_authorization_header_is_not_logged`、`test_json_formatter_does_not_emit_secrets`、`test_model_error_is_sanitized` |
| T14 | 容器内 root / 密钥入镜像 | 主机或密钥失陷 | 非 root uid 10001；密钥仅运行时环境变量；`.dockerignore` 排除 `.env` | CI `docker` job、`test_...`（部署检查） |
| T15 | 重启后任务状态混乱 | 伪造完成 | 启动把 planning/running 标记 `INTERRUPTED`；历史保留 | `test_recovery_marks_running_tasks_interrupted` |
| T16 | 备份不可用/数据损坏 | 数据丢失 | SQLite 在线 backup API；verify/restore 测试 | `test_backup_restore_roundtrip` |
| T17 | 明文 HTTP 暴露 | 凭证被窃听 | Compose 内置 Caddy HTTPS（本地内部 CA）；生产替换受信证书 | CI `compose-https` job |

## 4. 明确不覆盖

- 无企业 SSO/OIDC、无多租户隔离、无 RBAC 管理后台；
- 无 WAF、DDoS 防护、密钥托管（Vault/KMS）；
- 无 Kubernetes/多节点一致性；
- 无模型输出内容安全审核（只做结构化与来源校验）；
- 无真实企业系统写操作（本项目只读）。

## 5. 残余风险

1. API key 为静态 Bearer，泄漏后直到轮换前有效；
2. 模型仍可能生成错误但“有引用”的结论，审核只能降低概率；
3. 审核 Agent 本身也可能失败，此时任务降级为 `needs_evidence`/`partial`，不会伪成功；
4. 单节点 SQLite 仍是单点；
5. HTTPS 本地使用 Caddy 内部 CA，仅用于测试；生产必须配置受信任证书与主机名。
