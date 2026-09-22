# 东盟需求撮合枢纽

面向东博会的可独立运行跨境需求撮合服务：把"展示供给"转为"提前收集采购需求"，
并在预算、决策权、行业限制与供应商能力之间提供**可解释**的匹配过程。

零第三方依赖，仅使用 Python 3.11+ 标准库。

## 运行

```bash
python3 -m service.main                      # 事件日志默认 data/hub_events.jsonl
HUB_EVENT_LOG=/path/to/events.jsonl python3 -m service.main   # 自定义日志路径
python3 -m unittest discover -s tests        # 运行全部 51 个测试
```

## 关键能力

- **身份分离**：采购方实名资格独立保管，公开需求只有匿名代号；staff 才可查实名。
- **轮次冻结**：开放轮次内修订计 revision；冻结后修订开新轮，历史轮次不可变。
- **防重/联合响应**：牵头方不可重复响应、成员不可跨联合方案；提交支持幂等键。
- **多维匹配**：国家准入规则、行业、交付窗口、语言、利益冲突为硬约束；
  通过后按固定权重分项评分，逐项可解释。
- **人工留痕**：人工纳入/排除/置顶必须带操作人与原因，保存调整前后名次；
  产生预算承诺后整轮锁定。
- **增量重算**：候选退出只重算受影响关系，保留历史各代推荐。
- **预算安全**：洽谈预留、确认承诺；全局锁 + 幂等确认保证并发下不重复承诺。
- **重启恢复**：事件溯源（追加 JSONL + fsync），重放即还原全部轮次与洽谈状态。
- **脱敏解释**：供应商只看到自身评估与预算上限，任何解释都不含竞争报价。

## HTTP 接口概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/buyers/{buyer_ref}` | 实名登记（独立保险库） |
| POST | `/buyers/{buyer_ref}/verify` | 资格核验 |
| GET | `/buyers/{buyer_ref}`（X-Role: staff） | 查看实名信息 |
| POST | `/suppliers` | 供应商注册（能力/窗口/语言/自报冲突） |
| POST | `/requirements` | 发布需求（自动开第 1 轮） |
| POST | `/requirements/{id}/revise` | 修订（冻结后自动开新轮） |
| POST | `/requirements/{id}/freeze` | 冻结当前轮 |
| POST | `/requirements/{id}/responses` | 供应商响应（支持 `Idempotency-Key`、`members` 联合方案） |
| POST | `/responses/{id}/amend` `/withdraw` | 报价修订 / 退出（触发增量重算） |
| GET | `/responses/{id}/explanation` | 供应商视角脱敏解释 |
| POST | `/requirements/{id}/recommendations` | 生成推荐（自动冻结） |
| GET | `/requirements/{id}/rounds/{n}/recommendations` | 查看推荐（按 X-Role 隐去报价） |
| POST | `/requirements/{id}/rounds/{n}/overrides` | 人工调整（X-Actor + reason 必填） |
| GET | `/requirements/{id}/rounds/{n}/budget` | 预算台账（staff/buyer） |
| GET | `/requirements/{id}/rounds/{n}/explain/{candidate}` | 决策说明（staff/buyer） |
| POST | `/requirements/{id}/negotiations` | 开启洽谈（预留预算） |
| POST | `/negotiations/{id}/counter` `/confirm` `/close` | 还价 / 确认（幂等）/ 关闭释放 |

业务数据与敏感配置应放在受控运行环境（`HUB_EVENT_LOG` 指定持久化卷上的路径）。
领域规则详见 [docs/domain.md](docs/domain.md)。
