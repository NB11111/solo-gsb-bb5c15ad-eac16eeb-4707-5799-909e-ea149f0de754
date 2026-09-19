# 匿名评审分配 API（Anonymous Review Assignment）

基于 Python + FastAPI 的评审分配服务。`POST /assign` 将「稿件—评审人」分配问题整体规约为
**全局最大流**（Edmonds-Karp，非逐稿贪心），一次求出同时满足以下全部约束的完整分配：

- 每篇稿件获得指定数量的评审人；
- 评审人主题能力与稿件主题匹配；
- 回避利益冲突（含自稿冲突）；
- 评审人分配总量不超过其容量；
- 稿件带 `track`（赛道）时，还须满足评审人 `track_capacities` 中对应赛道的限额
  （评审人未列出的赛道只受总容量约束）。

赛道限额与总容量在**同一张流网络中串联**（赛道节点 → 评审人 → 汇点两级容量），
普通最大流与增量最小费用流都一次同时满足，绝不分赛道独立求解后拼接。

无可行解时返回未满足稿件及候选/容量缺口诊断，**不输出任何部分分配结果**；
相同输入（甚至不同的列表顺序）必然产生完全相同的输出。服务无状态，不持久化任何数据。

请求携带 `previous_assignments` / `locked_assignments` 时进入**增量重排模式**：
锁定关系经稿件需求、评审资格、总容量与赛道限额校验后强制保留（任一冲突即整体不可行
并返回结构化原因），其余名额在保证全局可行的前提下以**最小费用最大流**求解，
使相对上一轮的**新增 + 移除关系总数最少**（历史关系仅在赛道限额与总容量两级均允许时
才会被复用），同成本解按 ID 顺序确定性破除。

## 快速启动（单条命令）

```bash
docker compose up --build
```

依赖安装与初始化全部在镜像构建与容器启动时完成，无需任何额外步骤。

启动后访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/health | 健康检查 |
| http://localhost:8000/assign | 分配接口（POST） |
| http://localhost:8000/docs | Swagger 交互式文档 |
| http://localhost:8000/redoc | ReDoc 文档 |

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | 容器内服务监听端口 |

宿主机端口映射在 `docker-compose.yml` 的 `ports` 中调整（默认 `8000:8000`）。

不使用 Compose 时也可直接构建运行：

```bash
docker build -t review-assign .
docker run -p 8000:8000 review-assign
```

## API 说明

### `GET /health`

```json
{"status": "ok"}
```

### `POST /assign`

请求体：

```json
{
  "required_reviewers": 2,
  "manuscripts": [
    {"id": "m1", "authors": ["alice"], "topics": ["ml"], "track": "ml"},
    {"id": "m2", "authors": ["bob"], "topics": ["ml", "db"], "required_reviewers": 1, "track": "db"}
  ],
  "reviewers": [
    {"id": "r1", "topics": ["ml"], "capacity": 2, "track_capacities": {"ml": 2}},
    {"id": "r2", "topics": ["ml", "db"], "capacity": 1, "conflicted_authors": ["alice"],
     "track_capacities": {"ml": 1, "db": 0}},
    {"id": "r3", "topics": ["db"], "capacity": 2, "conflicted_manuscripts": ["m1"]},
    {"id": "r4", "topics": ["ml"], "capacity": 1}
  ]
}
```

字段规则：

- `required_reviewers`（请求级，默认 `1`）：每稿默认所需评审数；稿件级
  `required_reviewers` 可对其覆盖。
- 稿件 `topics` 非空时，评审人须与其有交集才具备能力；稿件 `topics` 为空则不做主题限制。
- 分赛道容量（均为可选，省略时行为与旧版本完全一致）：
  - 稿件可携带 `track`（非空字符串）。带赛道 `t` 的稿件分给评审人 `r` 时，
    若 `r.track_capacities` 中出现 `t`，则同时占用 `t` 的限额与 `r.capacity` 总容量；
    若未出现 `t`，则只占用总容量（即“未列出 = 不限赛道，仅有总量约束”）。
  - `track_capacities` 为 `赛道 -> 非负整数限额` 的映射；限额 `0` 表示该评审人
    不评审该赛道稿件。不带 `track` 的稿件永远不占用任何赛道限额。
  - 总容量与赛道限额在同一张流网络中串联校验：分赛道用量之和可以达到总容量，
    但总量不得超过 `capacity`。
- 冲突回避（满足任一即不可评审）：
  - 评审人 `id` 出现在稿件 `authors` 中（自稿冲突）；
  - 稿件作者与评审人 `conflicted_authors` 有交集；
  - 稿件 `id` 出现在评审人 `conflicted_manuscripts` 中。
- 稿件 / 评审人 `id` 必须各自唯一，否则返回 `422`。

可行时响应（`200`）：

```json
{
  "status": "feasible",
  "total_required": 3,
  "total_assigned": 3,
  "assignments": [
    {"manuscript_id": "m1", "reviewer_ids": ["r1", "r4"]},
    {"manuscript_id": "m2", "reviewer_ids": ["r1"]}
  ],
  "unsatisfied": null,
  "shared_pool": null,
  "locked_conflicts": null
}
```

不可行时响应（`200`，`assignments` 为 `null`，不输出部分结果）：

```json
{
  "status": "infeasible",
  "total_required": 4,
  "total_assigned": 2,
  "assignments": null,
  "unsatisfied": [
    {
      "manuscript_id": "m2",
      "required": 2,
      "assigned": 0,
      "missing": 2,
      "eligible_reviewers": 2,
      "eligible_remaining_capacity": 0,
      "candidate_gap": 0,
      "capacity_gap": 2
    }
  ],
  "shared_pool": {
    "manuscript_ids": ["m2"],
    "reviewer_ids": ["r1", "r2"],
    "missing_total": 2,
    "pool_remaining_capacity": 0,
    "pool_total_capacity": 2
  },
  "locked_conflicts": null
}
```

诊断字段含义：

- `eligible_reviewers`：通过主题与冲突过滤的合格候选人数；
- `eligible_remaining_capacity`：最大流结束后这些候选的剩余容量之和；
- `candidate_gap`：候选人数缺口（`required - eligible_reviewers`，候选本身不足）；
- `capacity_gap`：容量缺口（`missing - eligible_remaining_capacity`，候选够但容量不足）；
- `shared_pool`：所有未满足稿件共享的候选池及其总/剩余容量，用于定位整体瓶颈。

### 增量重排模式（可选）

请求携带 `previous_assignments` 和/或 `locked_assignments` 即进入增量重排模式
（两者都不携带时行为与上述普通模式完全一致）：

```json
{
  "required_reviewers": 1,
  "manuscripts": [
    {"id": "m1", "authors": ["alice"], "topics": ["ml"], "required_reviewers": 2},
    {"id": "m2", "authors": ["bob"], "topics": ["db"]}
  ],
  "reviewers": [
    {"id": "r1", "topics": ["ml"], "capacity": 2},
    {"id": "r2", "topics": ["ml", "db"], "capacity": 1},
    {"id": "r3", "topics": ["db"], "capacity": 1}
  ],
  "previous_assignments": [
    {"manuscript_id": "m1", "reviewer_ids": ["r1", "r2"]},
    {"manuscript_id": "m2", "reviewer_ids": ["r2"]}
  ],
  "locked_assignments": [
    {"manuscript_id": "m1", "reviewer_ids": ["r1"]}
  ]
}
```

字段规则：

- `previous_assignments`：上一轮分配结果。求解在保证全局可行的前提下尽量复用这些
  关系，使**新增 + 移除的关系总数最少**；引用已不存在的稿件/评审人、或当前已不满足
  资格/约束的关系自动视为无法复用（计为移除），不影响求解。
- `locked_assignments`：必须原样保留的锁定关系。每条锁定关系必须同时满足：
  - 稿件需求：同一稿件的锁定人数不超过其 `required_reviewers`；
  - 评审资格：与普通分配完全相同的主题匹配与冲突回避校验；
  - 总容量：同一评审人的锁定篇数不超过其 `capacity`；
  - 赛道限额：同一评审人在任一赛道（仅统计带该赛道、且评审人列出了该赛道的锁定稿件）
    的锁定篇数不超过其 `track_capacities` 中该赛道限额。

  任一锁定关系违反上述约束，即整体判定不可行（`200` + `status: "infeasible"`），
  返回结构化的 `locked_conflicts` 且**不输出任何部分分配**。
- 两个字段均为「稿件 -> 评审人列表」的条目数组；同一稿件可拆多条，重复关系自动去重。
- 赛道约束下的历史复用：带赛道稿件与评审人之间的历史关系，只有在赛道限额与总容量
  两级都仍允许时才会被复用；否则在最小费用流中自然放弃（计为移除），由全局最优替代。

上述例子的可行响应（`m1->r1` 被锁定保留，`m1->r2` 复用上一轮；`r2` 容量被 `m1` 占满，
`m2` 被迫由 `r2` 调整为 `r3`，共 1 新增 1 移除，已是最少改动）：

```json
{
  "status": "feasible",
  "total_required": 3,
  "total_assigned": 3,
  "assignments": [
    {"manuscript_id": "m1", "reviewer_ids": ["r1", "r2"]},
    {"manuscript_id": "m2", "reviewer_ids": ["r3"]}
  ],
  "unsatisfied": null,
  "shared_pool": null,
  "locked_conflicts": null
}
```

锁定冲突时的响应（`200`，`assignments` 为 `null`，不输出部分结果）：

```json
{
  "status": "infeasible",
  "total_required": 3,
  "total_assigned": 0,
  "assignments": null,
  "unsatisfied": null,
  "shared_pool": null,
  "locked_conflicts": [
    {
      "type": "reviewer_capacity_exceeded",
      "manuscript_id": null,
      "reviewer_id": "r1",
      "locked": 3,
      "limit": 2,
      "detail": "评审人 r1 被锁定 3 篇稿件，超出其容量 2"
    }
  ]
}
```

`locked_conflicts[].type` 取值：

| type | 含义 |
| --- | --- |
| `unknown_manuscript` / `unknown_reviewer` | 锁定关系引用了不存在的稿件 / 评审人 |
| `self_conflict` | 评审人是稿件作者（自稿冲突） |
| `author_conflict` | 评审人与稿件作者存在利益冲突 |
| `manuscript_conflict` | 评审人已声明回避该稿件 |
| `topic_mismatch` | 评审人主题能力与稿件主题不匹配 |
| `reviewer_capacity_exceeded` | 评审人锁定篇数超出总容量（`locked` / `limit` 给出数值） |
| `reviewer_track_capacity_exceeded` | 评审人在某赛道的锁定篇数超出赛道限额（`track` / `locked` / `limit` 给出赛道与数值） |
| `manuscript_requirement_exceeded` | 稿件锁定人数超出所需评审数（`locked` / `limit` 给出数值） |

增量模式下若锁定关系全部合法但整体仍不可行（容量/候选不足），响应与普通模式
不可行情形相同：`unsatisfied` + `shared_pool` 缺口诊断，`locked_conflicts` 为 `null`。

### 分赛道容量（可选）

为稿件指定 `track`、为评审人指定 `track_capacities` 即可启用两级容量。
下例中 `r1` 总容量 3，但每个赛道最多评审 1 篇：两篇 `ml` 稿件无法都分给 `r1`，
全局求解会把其中一篇分给 `r2`；`db` 稿件则仍可由 `r1` 评审（与 `ml` 各占 1）。

```json
{
  "required_reviewers": 1,
  "manuscripts": [
    {"id": "m1", "authors": ["alice"], "topics": ["ml"], "track": "ml"},
    {"id": "m2", "authors": ["bob"],   "topics": ["ml"], "track": "ml"},
    {"id": "m3", "authors": ["carol"], "topics": ["db"], "track": "db"}
  ],
  "reviewers": [
    {"id": "r1", "topics": ["ml", "db"], "capacity": 3,
     "track_capacities": {"ml": 1, "db": 1}},
    {"id": "r2", "topics": ["ml"], "capacity": 3,
     "track_capacities": {"ml": 1}}
  ]
}
```

```json
{
  "status": "feasible",
  "total_required": 3,
  "total_assigned": 3,
  "assignments": [
    {"manuscript_id": "m1", "reviewer_ids": ["r1"]},
    {"manuscript_id": "m2", "reviewer_ids": ["r2"]},
    {"manuscript_id": "m3", "reviewer_ids": ["r1"]}
  ],
  "unsatisfied": null,
  "shared_pool": null,
  "locked_conflicts": null
}
```

锁定关系超出赛道限额时返回新的结构化冲突类型（同样不输出任何部分分配）：

```json
{
  "status": "infeasible",
  "total_required": 2,
  "total_assigned": 0,
  "assignments": null,
  "unsatisfied": null,
  "shared_pool": null,
  "locked_conflicts": [
    {
      "type": "reviewer_track_capacity_exceeded",
      "manuscript_id": null,
      "reviewer_id": "r1",
      "track": "ml",
      "locked": 2,
      "limit": 1,
      "detail": "评审人 r1 在赛道 ml 被锁定 2 篇稿件，超出其该赛道限额 1"
    }
  ]
}
```

### 示例调用

```bash
curl -s http://localhost:8000/health

curl -s -X POST http://localhost:8000/assign \
  -H 'Content-Type: application/json' \
  -d '{
        "required_reviewers": 2,
        "manuscripts": [
          {"id": "m1", "authors": ["alice"], "topics": ["ml"]},
          {"id": "m2", "authors": ["bob"],   "topics": ["db"]}
        ],
        "reviewers": [
          {"id": "r1", "topics": ["ml"], "capacity": 2},
          {"id": "r2", "topics": ["ml", "db"], "capacity": 2},
          {"id": "r3", "topics": ["db"], "capacity": 1}
        ]
      }'

# 增量重排：锁定 m1->r1，并在全局可行前提下最少改动地复用上一轮分配
curl -s -X POST http://localhost:8000/assign \
  -H 'Content-Type: application/json' \
  -d '{
        "required_reviewers": 1,
        "manuscripts": [
          {"id": "m1", "authors": ["alice"], "topics": ["ml"], "required_reviewers": 2},
          {"id": "m2", "authors": ["bob"],   "topics": ["db"]}
        ],
        "reviewers": [
          {"id": "r1", "topics": ["ml"], "capacity": 2},
          {"id": "r2", "topics": ["ml", "db"], "capacity": 1},
          {"id": "r3", "topics": ["db"], "capacity": 1}
        ],
        "previous_assignments": [
          {"manuscript_id": "m1", "reviewer_ids": ["r1", "r2"]},
          {"manuscript_id": "m2", "reviewer_ids": ["r2"]}
        ],
        "locked_assignments": [
          {"manuscript_id": "m1", "reviewer_ids": ["r1"]}
        ]
      }'

# 分赛道容量：r1 总量 3 但 ml 赛道最多 1 篇，两篇 ml 稿件会被全局分流
curl -s -X POST http://localhost:8000/assign \
  -H 'Content-Type: application/json' \
  -d '{
        "manuscripts": [
          {"id": "m1", "authors": ["alice"], "topics": ["ml"], "track": "ml"},
          {"id": "m2", "authors": ["bob"],   "topics": ["ml"], "track": "ml"}
        ],
        "reviewers": [
          {"id": "r1", "topics": ["ml"], "capacity": 3,
           "track_capacities": {"ml": 1}},
          {"id": "r2", "topics": ["ml"], "capacity": 3,
           "track_capacities": {"ml": 1}}
        ]
      }'
```

## 算法与确定性

- 建图：`源 -> 稿件（容量=所需评审数）-> 评审人（容量=1，仅合格）-> 汇（容量=评审容量）`；
  最大流等于总需求即存在满足全部约束的分配，取其流即为解。
- 分赛道容量：为每个「评审人 × 其列出且被使用的赛道」建一个赛道节点。带该赛道的
  稿件连到赛道节点（容量 1），赛道节点经**一条**容量为赛道限额的边连到评审人，
  评审人再经容量为总容量的边连汇点。两级容量在同一条增广路径上串联，任一单位流量
  都同时通过赛道限额与总容量，因此普通最大流与增量最小费用流都**一次整体满足**
  两级约束，不会分赛道独立求解后拼接。未列出该赛道的评审人 / 不带赛道的稿件
  仍直连评审人节点，只受总容量约束。
- 全局求解保证整体可行（逐稿贪心无法保证，例如 m1 可选 r1/r2、m2 仅可选 r1 且容量均为 1 时，
  贪心会失败而全局解为 m1→r2、m2→r1）。
- 增量重排：锁定关系先逐一校验稿件需求、评审资格、总容量与赛道限额（任一冲突即整体
  不可行，返回结构化 `locked_conflicts`，含赛道维度的 `reviewer_track_capacity_exceeded`），
  合法锁定对固化后扣减稿件剩余需求、评审剩余总容量与赛道剩余限额；其余名额用最小费用
  最大流（SSP + 势函数）求解——复用 `previous_assignments` 的边费用为 0、新增边费用为 1，
  流量固定时费用最小即复用最多，亦即新增 + 移除关系总数最少；历史关系只有在赛道限额
  与总容量两级都允许（边可增广）时才能被复用。
- 所有节点按 ID（赛道节点再按赛道名）排序后建图，BFS / Dijkstra 均按固定邻接顺序与
  (费用, 节点下标) 字典序执行，算法无随机性：相同输入（含不同的列表顺序）产生
  逐字节相同的输出，同成本解由 ID 顺序确定性地破除。

## 本地开发与测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                 # 运行测试
uvicorn app.main:app --reload --port 8000   # 本地起服务
```

## 项目结构

```
app/
  main.py      # FastAPI 入口：/health、/assign
  models.py    # 请求/响应模型与校验
  solver.py    # 分配求解器：普通模式全局最大流 + 增量模式最小费用最大流
tests/
  test_assign.py
Dockerfile
docker-compose.yml
requirements.txt
requirements-dev.txt
```
