# Redacted Records API

供机构公开发布脱敏记录的 HTTP 服务。维护人员凭服务密钥提交原文与敏感区间，发布时按 Unicode 码点区间合并遮蔽，生成**不可变快照**与**随机读取令牌**；公众仅凭有效令牌读取脱敏正文。

自 1.1 起支持**受控发布**：机构可把单条记录设为受控，受控记录的直接发布被拒绝，必须由操作者以**个人凭据与身份**提交指定历史版本的发布申请，再由**另一名具复核资格的人**批准或驳回（申请人不得复核）。批准沿用既有脱敏与令牌规则，在同一事务生成不可变快照与新读取令牌。

- 技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16
- 服务地址：`http://localhost:8000`（交互式文档：`http://localhost:8000/docs`）

## 一键启动

```bash
docker compose up --build
```

Compose 会启动 PostgreSQL 与 API 两个容器，自动完成依赖安装与数据库初始化（启动时幂等建表，并重试等待数据库就绪）。数据持久化在命名卷 `pgdata` 中。

停止并清空数据：`docker compose down -v`

## 配置（环境变量）

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `SERVICE_KEY` | 维护人员服务密钥，**必填**，未配置服务拒绝启动 | 无（Compose 中为演示值 `dev-service-key-change-me`，生产务必修改） |
| `DATABASE_URL` | PostgreSQL 连接串 | `postgresql+psycopg2://redact:redact@localhost:5432/redact` |
| `DB_CONNECT_RETRIES` | 启动时等待数据库的重试次数 | `30` |
| `DB_CONNECT_RETRY_DELAY_SECONDS` | 重试间隔（秒） | `1.0` |
| `OPERATOR_CREDENTIALS` | 受控发布操作者凭据目录（JSON，见下） | 内置演示目录 `alice` / `bob` / `carol`，**生产必须覆盖** |

维护接口通过请求头 `X-Service-Key` 鉴权；缺失或不符一律 `401` 整次拒绝。

### 操作者凭据目录 `OPERATOR_CREDENTIALS`

JSON 对象，键为操作者 ID，值含个人凭据 `key` 与角色 `roles`（`operator` 可提交申请，`reviewer` 可批准/驳回，两者可兼具）：

```json
{
  "alice": {"key": "alice-secret", "roles": ["operator", "reviewer"]},
  "bob":   {"key": "bob-secret",   "roles": ["operator"]},
  "carol": {"key": "carol-secret", "roles": ["reviewer"]}
}
```

受控工作流用请求头 `X-Operator-Id` + `X-Operator-Key` 标识**操作者本人身份**（区别于机构级 `X-Service-Key`）；未知操作者或凭据不符一律 `401`，角色不符 `403`。内置演示目录仅供本地/Compose 使用。

## API 一览

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/records` | 服务密钥 | 创建记录（版本 1），提交原文与敏感区间 |
| `PUT` | `/records/{record_id}` | 服务密钥 | 修订：携带 `base_version`，过期返回 `409` 与当前版本 |
| `POST` | `/records/{record_id}/controlled` | 服务密钥 | 将单条记录设为/取消受控发布 `{"controlled": true}` |
| `POST` | `/records/{record_id}/publish` | 服务密钥 | 直接发布指定版本；**受控记录一律 `403` 拒绝** |
| `POST` | `/records/{record_id}/publish-requests` | 操作者 `operator` | 提交指定历史版本的受控发布申请 |
| `GET` | `/publish-requests/{request_id}` | 任一已认证操作者 | 查询申请状态（仅元数据，无正文/令牌） |
| `POST` | `/publish-requests/{request_id}/approve` | 另一复核者 `reviewer` | 批准：同事务生成不可变快照 + 新读取令牌 |
| `POST` | `/publish-requests/{request_id}/reject` | 另一复核者 `reviewer` | 驳回（终态，驳回后不能批准） |
| `POST` | `/tokens/{token}/revoke` | 服务密钥 | 撤销读取令牌 |
| `GET` | `/records/{record_id}` | 服务密钥 | 查看当前版本、受控标志与快照列表 |
| `GET` | `/public/snapshots/{snapshot_id}?token=...` | 无（凭令牌） | 公开读取脱敏正文 |
| `GET` | `/healthz` | 无 | 健康检查 |

### 区间语义

- 左闭右开 `[start, end)`，位置按 **Unicode 码点**计（与 Python 字符串下标一致，BMP 之外的字符算 1 个码点）。
- 越界、为空或倒置的区间 → `422`，**整次拒绝**，不落库任何内容。
- 发布时合并**相交或相邻**的区间，每段替换为 `[已遮蔽]`，其余原文保留。

### 调用示例

```bash
# 1. 创建记录（版本 1）
curl -sS -X POST http://localhost:8000/records \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{
        "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
        "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
      }'
# => {"record_id":"<RID>","version":1}

# 2. 修订（base_version 过期时返回 409 与当前版本）
curl -sS -X PUT http://localhost:8000/records/<RID> \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"base_version": 1, "text": "……新原文……", "intervals": [{"start": 0, "end": 3}]}'
# => {"record_id":"<RID>","version":2}
# 过期时 => 409 {"detail":{"message":"base_version is stale","current_version":2}}

# 3. 发布版本 2（可另加 "expires_in_seconds": 3600 或 "expires_at": "2026-10-01T00:00:00Z"）
curl -sS -X POST http://localhost:8000/records/<RID>/publish \
  -H "X-Service-Key: dev-service-key-change-me" \
  -H "Content-Type: application/json" \
  -d '{"version": 2}'
# => {"snapshot_id":"<SID>","version":2,"status":"published","token":"<TOKEN>","expires_at":null}

# 4. 公开读取（无需密钥，只返回脱敏正文）
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"
# => {"record_id":"<RID>","version":2,"status":"published","redacted_text":"患者[已遮蔽]，……"}

# 5. 撤销令牌（撤销完成后，新开始的读取一律 403）
curl -sS -X POST http://localhost:8000/tokens/<TOKEN>/revoke \
  -H "X-Service-Key: dev-service-key-change-me"
```

完整端到端演示（含 401/409/422、快照不可变、撤销与到期）：

```bash
./examples/demo.sh        # 依赖 curl 与 jq；可用 BASE_URL / SERVICE_KEY 覆盖默认配置
```

## 受控发布工作流

适用规则：受控记录只能走“申请 → 他人复核”；非受控记录及既有创建/修订/读取/撤销接口行为不变。

**职责分离**：提交者必须具 `operator` 角色，复核者必须具 `reviewer` 角色，且**申请人本人不得复核**自己的申请（即使兼具两角色）。

**内容冻结**：申请绑定提交时的 `(记录, 版本, 原文 SHA-256 指纹, 区间副本)`。后续修订只追加新版本、不改写历史版本，因此待审内容不变；批准时再次核对指纹，不符则整笔回滚、绝不发布。

**终态唯一**：`pending → approved | rejected`。驳回后不能批准；同一申请的并发批准/驳回通过条件更新 `UPDATE ... WHERE status='pending'` 原子认领，只产生一个终态。

**原子批准**：批准沿用既有脱敏（区间合并为 `[已遮蔽]`）与令牌规则（`secrets.token_urlsafe(32)`、相同到期语义），在**同一事务**写入不可变快照、关联申请并签发新读取令牌；任一步失败整体回滚，不留下可读令牌。

```bash
# 1. 将记录设为受控（服务密钥）
curl -sS -X POST http://localhost:8000/records/<RID>/controlled \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"controlled": true}'
# => {"record_id":"<RID>","controlled":true}

# 2. 受控记录走既有直接发布 -> 403 明确拒绝
curl -sS -X POST http://localhost:8000/records/<RID>/publish \
  -H "X-Service-Key: dev-service-key-change-me" -H "Content-Type: application/json" \
  -d '{"version": 1}'
# => 403 {"detail":"direct publish is disabled for controlled records"}

# 3. bob 用个人凭据提交“指定历史版本”的发布申请（可带 expires_in_seconds / expires_at）
curl -sS -X POST http://localhost:8000/records/<RID>/publish-requests \
  -H "X-Operator-Id: bob" -H "X-Operator-Key: bob-key" -H "Content-Type: application/json" \
  -d '{"version": 1}'
# => {"request_id":"<PRID>","status":"pending","submitted_by":"bob","snapshot_id":null,...}

# 4. 申请人自审或无 reviewer 角色 -> 403；未知/缺凭据 -> 401
# 5. 另一名复核者 carol 批准（驳回用 /reject，可带 {"note":"..."}）
curl -sS -X POST http://localhost:8000/publish-requests/<PRID>/approve \
  -H "X-Operator-Id: carol" -H "X-Operator-Key: carol-key" -H "Content-Type: application/json" \
  -d '{}'
# => {"request_id":"<PRID>","status":"approved","snapshot_id":"<SID>","token":"<TOKEN>",...}

# 6. 用新令牌公开读取（与既有读取接口一致）
curl -sS "http://localhost:8000/public/snapshots/<SID>?token=<TOKEN>"

# 7. 查询申请状态（任何角色认证后均可；响应不含正文与令牌）
curl -sS http://localhost:8000/publish-requests/<PRID> \
  -H "X-Operator-Id: bob" -H "X-Operator-Key: bob-key"

# 已终结申请再批准/驳回 -> 409；驳回后批准 -> 409；未知记录/版本/申请 -> 404
```

状态码约定：

| 场景 | 状态码 |
| --- | --- |
| 缺少/错误操作者凭据、未知操作者 | `401` |
| 角色不符、**申请人复核本人申请**、受控记录直接发布 | `403` |
| 记录/版本/申请不存在 | `404` |
| 非受控记录提交受控申请 | `409` |
| 申请已终结（已批准/已驳回）再复核 | `409` |
| 到期时刻在过去、区间越界 | `422` |

所有错误信息只描述失败原因，**不含原文或脱敏正文**；批准生成的令牌与普通令牌一样可被 `POST /tokens/{token}/revoke` 撤销。

完整受控流程演示：

```bash
./examples/controlled_publish.sh   # 依赖 curl 与 jq；可用 BASE_URL / SERVICE_KEY 覆盖
```

## 设计要点

- **不可变快照**：发布时把脱敏结果写入 `snapshots`（按 `(record_id, version)` 唯一），公开读取只访问快照；后续修订写入新版本，旧快照不变。受控批准复用同一快照表（该版本已有快照则关联同一份，绝不重复落库）。
- **受控发布**：
  - 记录新增 `controlled` 标志（既有记录缺省 `false`，行为兼容）；受控后 `POST /publish` 直接 `403`；
  - 申请存于 `publish_requests`，冻结 `record_id + version + content_hash + 区间副本 + 到期时刻 + submitted_by`；批准时核对版本仍存在且指纹一致，否则整笔回滚不发布；
  - 职责分离：`operator` 提交、另一 `reviewer` 复核，申请人本人一律不得复核；
  - 终态机：仅 `pending` 可流转；批准/驳回用条件更新原子认领 pending 行，并发复核只产生一个终态，驳回后不能批准；
  - 批准在单事务内完成「认领 → 快照（ON CONFLICT DO NOTHING）→ 新令牌 → 回写申请」，提交失败整体回滚，不会留下可读令牌。
- **并发安全**：
  - 修订用条件更新 `UPDATE ... WHERE current_version = :base_version`，原子比较并递增，并发修订只有一个成功，其余收到 `409` 与当前版本；
  - 发布用 `INSERT ... ON CONFLICT DO NOTHING` 保证同一版本只有一份快照，快照与令牌同事务提交，不存在部分发布；
  - 复核用条件更新 `UPDATE ... WHERE status='pending'` 保证终态唯一；撤销是单条原子 `UPDATE`，提交后新开始的读取即被拒绝。
- **不泄露原文**：公开接口只返回快照中的脱敏正文；申请接口只返回元数据（无原文、无脱敏正文、无令牌）；令牌无效/撤销/到期统一返回模糊的 `403 snapshot unavailable`；其余错误信息也不含原文内容。
- **令牌**：`secrets.token_urlsafe(32)` 随机生成；支持 `expires_in_seconds` 或 `expires_at`（二选一）设置到期时间，受控申请在提交时即把相对秒数换算为绝对 UTC 时刻冻结；到期或撤销后读取一律拒绝。

## 本地开发

```bash
pip install -r requirements.txt
export SERVICE_KEY=dev-service-key-change-me
export DATABASE_URL=postgresql+psycopg2://redact:redact@localhost:5432/redact
uvicorn app.main:app --reload --port 8000

# 脱敏核心逻辑单元测试（仅依赖标准库）
python3 -m unittest tests.test_redaction -v
# 受控发布工作流端到端测试（需 fastapi/httpx，可用 SQLite 内存库，无需 PostgreSQL）：
SERVICE_KEY=test DATABASE_URL='sqlite:///:memory:' python3 -m unittest tests.test_controlled_publish -v
```

## 目录结构

```
app/
  main.py       # 路由与事务逻辑（含受控发布申请/批准/驳回工作流）
  models.py     # records / record_versions / snapshots / read_tokens / publish_requests
  redaction.py  # 区间校验、合并、替换（纯函数）
  schemas.py    # 请求/响应模型
  security.py   # 服务密钥鉴权 + 操作者身份/角色依赖
  config.py     # 环境变量配置（含操作者凭据目录）
  db.py         # 引擎与会话
examples/demo.sh               # 直接发布端到端示例
examples/controlled_publish.sh # 受控发布复核工作流示例
tests/                         # 脱敏单元测试 + 受控工作流端到端测试
Dockerfile / docker-compose.yml
```
