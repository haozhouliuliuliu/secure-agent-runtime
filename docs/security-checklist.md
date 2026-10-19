# 安全测试清单

> 安全门槛：任意一项必须通过的检查失败，都不应视为可上线。证据来自自动化测试、CI 和真实模型验证。

## 1. 硬门槛对照

| # | 必须验证 | 测试/证据 | 结果 |
|---|---|---|---|
| H1 | Business 不能执行 `search_policies` | `test_tools.py::test_business_cannot_execute_policy_search`；`test_security.py::test_business_tool_denial_is_logged_but_allowed_call_succeeds` | 通过 |
| H2 | Policy 不能执行库存/工单工具 | `test_policy_cannot_query_inventory`、`test_policy_cannot_query_tickets` | 通过 |
| H3 | Auditor 不能执行任何工具 | `test_auditor_cannot_execute_any_tool`、`test_planner_and_auditor_have_no_tools` | 通过 |
| H4 | Planner 不能执行业务工具 | `test_planner_cannot_execute_tools` | 通过 |
| H5 | 模型请求的工具列表按角色裁剪 | `test_business_schema_only_contains_four_tools`、`test_policy_schema_only_contains_search` | 通过 |
| H6 | 未知/越权工具不返回数据并记录 | `test_unknown_tool_is_denied_for_business`、`test_business_unknown_tool_is_denied_without_data` | 通过 |
| H7 | Policy 模型失败时不得用 Business 替代制度检索 | `test_policy_model_failure_does_not_trigger_substitute_search`、`test_policy_cannot_read_business_data_and_fails_honestly` | 通过 |
| H8 | 未认证/错误密钥 401 | `test_missing_and_invalid_token_return_401`、`test_keystore_authentication_and_roles` | 通过 |
| H9 | 普通用户访问管理员接口 403 | `test_admin_can_read_any_task_and_executor_gets_403_on_admin_api` | 通过 |
| H10 | 跨用户任务访问返回一致 404 | `test_cross_user_access_returns_404` | 通过 |
| H11 | 任务列表按 owner 隔离 | `test_task_list_is_owner_scoped` | 通过 |
| H12 | 密钥/Authorization 不入日志 | `test_redact_removes_keys_and_authorization`、`test_json_formatter_does_not_emit_secrets`、`test_authorization_header_is_not_logged` | 通过 |
| H13 | 客户端错误不暴露供应商原文 | `test_model_error_is_sanitized` | 通过 |
| H14 | 并发不能绕过全局预算 | `test_global_budget_is_concurrency_safe`、`test_global_budget_stops_new_work_without_fake_success` | 通过 |
| H15 | 单任务预算独立生效 | `test_per_task_budget_is_isolated`、`test_per_task_model_budget_is_enforced` | 通过 |
| H16 | 过载/限流/超长输入明确拒绝 | `test_overload_returns_503`、`test_rate_limit_returns_429`、`test_oversized_input_is_rejected` | 通过 |
| H17 | 模型 401/429/5xx/超时/无效 JSON 正确降级 | `test_llm.py` 全套、`test_invalid_model_json_fails_without_fake_success`、`test_business_auth_error_no_retry` | 通过 |
| H18 | 10 并发任务数据不串线 | `test_ten_concurrent_tasks_do_not_cross_talk` | 通过 |
| H19 | 重启后历史可读、运行中任务标记中断 | `test_recovery_marks_running_tasks_interrupted`、`test_persistence_after_repository_reload` | 通过 |
| H20 | 备份/恢复可用 | `test_backup_restore_roundtrip` | 通过 |
| H21 | 注入文本不改变权限 | `test_ticket_injection_is_returned_as_data`、`test_untrusted_ticket_instruction_is_flagged` | 通过 |
| H22 | Docker 非 root + HTTPS + 未认证 401 | CI `docker` 与 `compose-https` job | 通过（见 CI 链接） |

## 2. 测试结果

- 自动化：**80 passed**；
- 覆盖率：**83%**（`pytest --cov-fail-under=80`）；
- Python 3.11 / 3.12 均通过；
- Docker 镜像构建、容器冒烟、Compose + Caddy HTTPS 冒烟由 CI 执行。

## 3. 人工检查项（每次发布）

- [ ] `.env` 未被提交（`git check-ignore .env`）；
- [ ] 镜像内无密钥：`docker history` / 环境变量仅运行时注入；
- [ ] `docker compose config` 不含明文默认密钥；
- [ ] 生产证书与主机名已替换 `tls internal`；
- [ ] 备份文件访问权限仅限运维；
- [ ] 任务保留期与删除流程已确认；
- [ ] 管理员 API key 与普通 key 分开轮换。

## 4. 真实模型验证

见 [testing.md](testing.md)：验证受控工具不影响正常任务、Policy 故障时报告明确不可用、
所有工具调用符合角色白名单、真实调用次数 ≤25。
