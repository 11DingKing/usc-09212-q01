# 东盟需求撮合枢纽

东博会从"展示供给"转向"提前收集东盟采购需求"后的跨境需求撮合服务。纯 Python 标准库实现、
单进程可独立运行，采用事件溯源 + 仅追加日志，重启后可完整还原每轮推荐与洽谈状态。

## 能力

- 采购方**实名资格**与公开需求**分离保存**；公开侧仅出现匿名 `buyer_code`
- 需求修订按轮次进行，轮次**冻结**后生成不可变快照
- 供应商响应**幂等防重复**，支持多成员**联合方案**（成员身份同样防重复）
- 匹配同时考虑国家准入、交付窗口、语言、行业、产能、预算与**利益冲突**
- 加权评分 + 硬约束原因，支持人工排除/调名/恢复且**全程留痕**
- 候选退出只**增量重算受影响关系**
- 预算持有（hold）→ 承诺（committed），并发确认下同一预算不重复承诺
- 三视角解释：向双方说明为何匹配或拒绝，**不泄露竞争报价**
- 服务重启后重放日志，还原每轮推荐快照与洽谈状态

## 运行与测试

```bash
python3 -m unittest discover -s tests     # 28 个测试
python3 -m service.main                   # 默认 127.0.0.1:8000，数据写 ./data
```

环境变量：`MATCH_HOST`、`MATCH_PORT`、`MATCH_DATA_DIR`（设为 `:memory:` 不落盘）。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/qualifications` | 采购方实名登记（写入独立资格日志） |
| POST | `/qualifications/{id}/verify` | 资格核验 |
| POST | `/suppliers` | 供应商目录登记（含利益冲突申报） |
| POST | `/rounds/open` `/rounds/{n}/freeze` | 开标 / 冻结出快照 |
| POST | `/demands`，PUT `/demands/{id}` | 需求提交 / 轮次内修订 |
| POST | `/responses`，POST `/responses/{id}/withdraw` | 响应（幂等、联合）/ 退出触发增量重算 |
| POST | `/rounds/{n}/recommendations` | 生成（或重算）推荐 |
| POST | `/manual-adjustments` | exclude / override_rank / reinstate，留痕 |
| POST | `/negotiations/start|offer|confirm|cancel` | 洽谈与预算持有、承诺 |
| GET | `/demands/{id}/explain?kind=buyer|supplier|operator&id=...` | 分视角可解释报告 |
| GET | `/audit?demand_id=...` | 人工操作留痕查询 |

写接口可携带业务 `event_time`（ISO-8601），缺省由服务端补；服务端另记 `received_at`。
领域规则与存储约定见 [docs/domain.md](docs/domain.md)。
