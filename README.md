# 合同费率双时态追溯 API

基于 **Python + FastAPI + SQLite** 的合同费率追溯服务。录入费率时同时保存
**业务生效区间**（valid time）与**系统记录时间**（system time）两个维度；
修订既有区间只追加新版本、旧版本标记取代，历史永不覆盖。

## 一键启动

```bash
docker compose up --build
```

依赖安装与数据库建表全部自动完成。启动后访问：

- API 入口： http://localhost:8000
- 交互式文档（Swagger UI）： http://localhost:8000/docs
- 健康检查： http://localhost:8000/health

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_PATH` | 容器内 `/data/rates.db`（本地直跑默认 `./rates.db`） | SQLite 数据库文件路径，启动时自动建表 |
| `ports` | `8000:8000` | 在 `docker-compose.yml` 中修改宿主机端口 |

数据通过 Compose 命名卷 `rates-data` 持久化，删除容器不丢数据。

## 双时态模型

每条费率版本记录两个时间区间（均左闭右开）：

| 维度 | 字段 | 含义 |
|---|---|---|
| 业务时间 | `valid_from` ~ `valid_to` | 费率在现实中生效的区间，`valid_to` 缺省为无限期 |
| 系统时间 | `recorded_at` ~ `superseded_at` | 该记录被系统认知为真的区间，`superseded_at` 为 NULL 表示当前认知 |

修订 = 插入新版本（`version` 递增）并把区间重叠的旧版本置上 `superseded_at`，
旧行不修改、不删除，任意历史认知时点均可回放。

## 接口

### 1. 录入 / 修订费率 — `POST /rates`

```bash
curl -X POST http://localhost:8000/rates \
  -H 'Content-Type: application/json' \
  -d '{
        "contract_no": "HT-2026-001",
        "rate": 0.052,
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to":   "2027-01-01T00:00:00Z",
        "recorded_at": "2026-01-01T09:00:00Z",
        "reason": "初始费率"
      }'
```

- `valid_to`、`recorded_at`、`reason` 均可选；`recorded_at` 缺省为当前时间。
- 与当前版本区间重叠时自动修订：响应中 `superseded_rate_ids` 列出被取代版本。
- 同一合同 `recorded_at` 必须单调递增，否则 `422 NON_MONOTONIC_RECORDED_AT`；
  `valid_to <= valid_from` 返回 `422 INVALID_INTERVAL`。

### 2. 报价 — `GET /quote`

```bash
curl "http://localhost:8000/quote?contract_no=HT-2026-001&valid_at=2026-03-01T00:00:00Z&known_at=2026-06-01T00:00:00Z"
```

返回该双时态坐标下唯一可见且有效的费率及版本依据（`basis` 含版本号、
记录时间、生效区间）。`known_at` 缺省为当前时间。

| 情形 | 状态码 | `code` |
|---|---|---|
| 恰好一条命中 | 200 | — |
| 无匹配 | 404 | `NO_MATCH` |
| 多条重叠命中 | 409 | `OVERLAP`（附 `candidates` 冲突明细） |

### 3. 版本时间线 — `GET /contracts/{contract_no}/timeline`

```bash
curl http://localhost:8000/contracts/HT-2026-001/timeline
```

按版本号列出全部版本及其业务/系统时间区间与状态（`current` / `superseded`）；
合同不存在返回 `404 CONTRACT_NOT_FOUND`。

### 4. 覆盖率分析 — `GET /contracts/{contract_no}/coverage`

```bash
curl "http://localhost:8000/contracts/HT-2026-001/coverage?from=2026-01-01T00:00:00Z&to=2027-01-01T00:00:00Z&known_at=2026-06-01T00:00:00Z"
```

先按 `recorded_at` / `superseded_at` 筛出 `known_at` 时点可见的版本，
再以这些版本业务区间的全部边界把查询窗口 `[from, to)` 切分为**连续**片段，
逐段标记：

| `status` | 含义 | 附带字段 |
|---|---|---|
| `covered` | 恰好一个可见版本覆盖该段 | `basis`：唯一版本依据 |
| `gap` | 无任何可见版本覆盖 | — |
| `conflict` | 多个可见版本重叠覆盖（脏数据） | `candidates`：全部候选版本 |

分类与候选完全相同的相邻片段会合并为一段。`known_at` 缺省为当前时间；
`from >= to` 返回 `422 INVALID_INTERVAL`；该认知时点下无任何可见版本时，
返回一个覆盖整个查询窗口的 `gap` 片段（合同不存在亦然，不报 404）。

响应示例（上半年已被修订取代、下半年被新版本覆盖）：

```json
{
  "contract_no": "HT-2026-001",
  "from": "2026-01-01T00:00:00.000000+00:00",
  "to": "2027-01-01T00:00:00.000000+00:00",
  "known_at": "2026-06-01T00:00:00.000000+00:00",
  "segments": [
    {"from": "2026-01-01T00:00:00.000000+00:00", "to": "2026-06-01T00:00:00.000000+00:00",
     "status": "gap", "basis": null, "candidates": []},
    {"from": "2026-06-01T00:00:00.000000+00:00", "to": "2027-01-01T00:00:00.000000+00:00",
     "status": "covered", "basis": {"rate_id": 2, "rate": 0.058, "version": 2, "...": "..."},
     "candidates": []}
  ]
}
```

## 本地开发（不用 Docker）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest                 # 运行测试
```

## 项目结构

```
app/
  main.py      FastAPI 路由与请求/响应模型
  logic.py     双时态核心逻辑（追加式录入、时点查询、时间线、覆盖率分段）
  db.py        SQLite 连接与自动建表
  timeutil.py  UTC 时间规范化（等宽 ISO 字符串，可字典序比较）
tests/test_api.py
Dockerfile / docker-compose.yml
```
