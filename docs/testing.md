# 测试与真实模型评估

> 基线：项目⑤ `v1.0.0` / `fb17bfc`。本仓库在其上做安全加固。

## 1. 自动化测试

```bash
pytest -q
pytest --cov=team_agent --cov-report=term   # 门槛 80%
```

最近结果：

- **80 passed**；
- 覆盖率 **83%**；
- Python 3.11 / 3.12（CI）；
- ruff check / format 通过。

| 类别 | 代表测试 |
|---|---|
| 工具授权 | 角色工具列表裁剪、执行前拒绝、未知工具、Auditor/Planner 无工具 |
| 认证/隔离 | 401、403、跨用户 404、列表隔离、管理员权限 |
| 注入 | 工单描述注入仅作为数据、审核 finding、Business 不能替代 Policy |
| 模型故障 | 401/429/5xx/超时/无效 JSON/不存在模型、失败降级 |
| 并发预算 | 20 线程抢 5 个全局名额、单任务独立、10 任务不串线 |
| 过载/限流 | 429、503、413 |
| 日志脱敏 | key/Authorization 不落日志、Formatter 脱敏 |
| 状态/恢复 | 重启标记 INTERRUPTED、历史保留 |
| 备份 | 在线备份、删除、恢复、校验 |
| 部署 | CI 构建镜像、非 root、HTTPS Compose 冒烟 |

硬门槛逐项对照见 [security-checklist.md](security-checklist.md)。

## 2. 真实模型评估

- 通过**受控 HTTP API**（TestClient 驱动真实 FastAPI）提交任务；
- 模型：`deepseek-flash`（Planner）、`qwen3.8-flash`（Business/Policy）、`kimi-k2.7-code`（Auditor）；
- 全局调用硬上限 25，实际 **16 次**；
- 认证检查不消耗模型调用。

### 2.1 结果

运行：`evidence/real-model/run-20261019-100445/`

| 用例 | 场景 | 状态 | 审核 | 工具 | 调用 |
|---|---|---|---|---|---|
| R1 | T14 库存直查 | completed | - | query_inventory | 3 |
| R2 | INC-2026-0001 工单直查 | completed | - | get_ticket | 3 |
| R3 | 新员工综合研判 | partial | needs_evidence | list_tickets, query_inventory, search_policies | 6 |
| R4 | Policy 模型 404 故障 | partial | 跳过 | list_tickets, query_inventory | 4 |

认证检查（不消耗模型调用）：

```json
{"no_token": 401, "wrong_token": 401, "executor_admin_api": 403, "missing_task": 404, "cross_user": 404}
```

总 token：约 37,000（R1 2,890；R2 2,493；R3 25,772；R4 5,881）。

### 2.2 证明了什么

1. **受控工具访问不影响正常任务**：R1/R2 在只读白名单下完成；
2. **角色权限未被破坏**：R3 的工具集合符合 Business（业务工具）+ Policy（`search_policies`）；
3. **Policy 不可用时不伪造制度结论**：R4 中 Policy 模型返回 HTTP 404，任务变 `partial`，
   `policy_unavailable=true`、`policy_summary=null`，最终报告没有制度条款；
4. **Business 无法替代制度检索**：R4 的工具集合中没有 `search_policies`（权限在程序层拒绝）；
5. **审核仍可运行**：R3 由 Kimi 审核并给出 `needs_evidence`。

### 2.3 没有证明什么

- 真实模型没有主动尝试越权工具（这类场景由 80 个模拟测试覆盖），因此不能把 R1–R4 当作"模型不会越权"的证据；
- 只覆盖 4 个场景，不代表所有业务问题；
- 审核结论仍是概率性的，`needs_evidence` 不等于业务结论正确；
- 真实调用 16 次，样本小，不构成统计意义上的质量评估。

## 3. 部署验证

- CI `docker` job：构建镜像、启动容器、检查 health、401、403、uid 10001；
- CI `compose-https` job：Compose 启动 Agent + Caddy，`curl -k https://localhost:8443/health/live`
  成功，未认证 `/v1/tasks` 返回 401，授权后返回 200；
- 备份恢复由 `test_backup_restore_roundtrip` 验证；
- CI 运行链接与提交号见最终交付说明。

## 4. 复现

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest -q --cov=team_agent --cov-fail-under=80

set DEEPSEEK_API_KEY=...
set QWEN_API_KEY=...
set KIMI_API_KEY=...
python -m eval.harness --output evidence/real-model
```

真实评估会自行生成测试 API key（仅内存），不依赖外部 `SERVICE_API_KEYS`。
