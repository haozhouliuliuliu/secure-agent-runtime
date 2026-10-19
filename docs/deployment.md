# 部署、HTTPS 与备份恢复

## 1. 本地 Docker Compose（HTTPS）

```bash
export SERVICE_API_KEYS='[{"key":"exec-key-change-me","user":"alice","role":"executor"},{"key":"admin-key-change-me","user":"admin","role":"admin"}]'
export DEEPSEEK_API_KEY=... QWEN_API_KEY=... KIMI_API_KEY=...
docker compose up --build -d

curl -k https://localhost:8443/health/live
curl -k -o /dev/null -w "%{http_code}\n" https://localhost:8443/v1/tasks   # 401
curl -k https://localhost:8443/v1/tasks \
  -H "Authorization: Bearer exec-key-change-me"
```

- Agent 监听容器内 8000，Caddy 监听 443，映射到宿主 8443；
- Caddy 使用 `tls internal` 生成本地 CA 证书，浏览器会提示不受信任，这是测试配置；
- Agent 以 uid 10001 运行，SQLite 位于命名卷 `agent-data`；
- 密钥只通过 compose 的环境变量传入，不进入镜像层。

## 2. 生产 HTTPS 建议

1. 使用受信任证书（ACME 或企业 CA），不要把 `tls internal` 当生产配置；
2. Caddyfile 改为正式域名：

```caddyfile
agent.example.com {
    reverse_proxy agent:8000
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options nosniff
        X-Frame-Options DENY
    }
}
```

3. 通过防火墙只暴露 443；8000 仅容器网络可达；
4. 在反向代理层增加请求体大小、超时、访问日志与速率限制；
5. 密钥建议接入 Vault/KMS 或容器 secret，而不是 `.env` 文件。

## 3. 健康检查与优雅关闭

- `/health/live`：进程存活；
- `/health/ready`：数据库可读 + API key 配置 + 四个模型配置齐全；
- Compose healthcheck 使用 `/health/live`；
- `docker compose stop` 发送 SIGTERM，uvicorn 停止接收新请求，服务在 lifespan 中关闭线程池；
- 重启时未完成的 `planning/running` 任务标记 `failed/INTERRUPTED`，不会伪完成。

## 4. 备份

```bash
# 在容器内备份到数据卷
docker compose exec agent python -m team_agent.backup_cli backup \
  --db /app/data/collab.db --out /app/data/backup-20261019.db

# 复制到宿主机
docker compose cp agent:/app/data/backup-20261019.db ./backup-20261019.db

# 校验
python -m team_agent.backup_cli verify --db ./backup-20261019.db
```

使用 SQLite 在线 backup API，不直接复制正在写入的 WAL 文件。

## 5. 恢复

```bash
docker compose stop agent
docker compose cp ./backup-20261019.db agent:/app/data/collab.db
docker compose start agent
curl -k https://localhost:8443/health/ready
```

恢复前先停止 Agent，避免并发写入；恢复后检查 `/health/ready` 与任务列表。

## 6. 保留与删除

- 默认保留 30 天（`SERVICE_TASK_RETENTION_DAYS`）；
- 管理员可 `DELETE /v1/tasks/{id}` 删除单个任务及轨迹；
- 批量清理：

```bash
python -m team_agent.backup_cli purge --db data/collab.db --days 30
```

删除会同时清理子任务、模型调用、交接、审核与事件；备份文件应单独设置保留与访问控制。

## 7. 恢复演练记录

自动化测试 `test_backup_restore_roundtrip` 会：

1. 生成一个已完成任务；
2. 在线备份；
3. 删除任务；
4. 校验备份表计数；
5. 恢复并确认任务重新可读。

CI 的 `compose-https` job 会真实启动 Compose + Caddy，并验证 HTTPS 健康检查与未认证 401。
