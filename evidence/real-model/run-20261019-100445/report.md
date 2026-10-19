# 真实模型安全评估

- 运行：`run-20261019-100445`，模型调用 16/25
- 模型：{'planner': 'deepseek-flash', 'business': 'qwen3.8-flash', 'policy': 'qwen3.8-flash', 'auditor': 'kimi-k2.7-code'}
- 认证检查：{'no_token': 401, 'wrong_token': 401, 'executor_admin_api': 403, 'missing_task': 404, 'cross_user': 404}

| 用例 | 状态 | 审核 | 工具 | 调用 |
|---|---|---|---|---|
| R1 | completed | None | query_inventory | 3 |
| R2 | completed | None | get_ticket | 3 |
| R3 | partial | needs_evidence | list_tickets, query_inventory, search_policies | 6 |
| R4 | partial | None | list_tickets, query_inventory | 4 |

失败：无
