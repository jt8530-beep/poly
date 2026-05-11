# arb-engine-v0

**Purpose:** 自动化、纯套利、零方向性。
当前实现：策略 C（Ladder 单调性套利）+ Telegram 通知 + 纸面交易记账。

---

## 策略 C 是什么

Polymarket 上很多事件是"阶梯"结构——同一底层事件 + 若干有序阈值（时间或数值）的二元合约。

示例（时间阶梯）：
```
Russia captures X by May 31   YES = 0.08
                      Jun 30   YES = 0.20
                      Jul 31   YES = 0.40
                      Aug 31   YES = 0.55
```
无套利约束：**时间越远 YES 应越贵，NO 应越便宜**。

示例（数值阶梯）：
```
BTC >= 100k by Y     YES = 0.45
BTC >= 110k by Y     YES = 0.20
BTC >= 120k by Y     YES = 0.08
```
无套利约束：**门槛越高 YES 应越便宜**。

当订单簿出现违反单调性（上一档 bid > 下一档 ask 超过阈值），我们：
- 卖出应便宜那档的 YES（hit its bid）
- 买入应更贵那档的 YES（lift its ask）
- 由于二元合约的赔付结构，**两腿组合的最终 PnL 在所有世界中非负**

这就是方向无关的"真套利"。

---

## 代码结构

```
arb/
├── __init__.py
├── config.py          # 所有参数都从环境变量读
├── poly_client.py     # Gamma + CLOB 只读客户端 (urllib, 零依赖)
├── ladder.py          # 策略 C 的主逻辑（发现事件 → 排序 → 检测违反）
├── risk.py            # 仓位、止损、回撤断路器
├── ledger.py          # SQLite 记账
├── tg_notifier.py     # Telegram 通知
├── executor.py        # 实盘下单（v0 故意留空，保护你）
└── main.py            # 主循环
scripts/
└── deploy_oracle.sh   # 一键部署到甲骨文 VPS
```

---

## 风控默认值（环境变量覆盖）

| 项 | 默认 | 环境变量 |
|---|---|---|
| 单笔最大名义 | $30 | `RISK_MAX_NOTIONAL_PER_TRADE` |
| 总敞口上限 | $200 | `RISK_MAX_OPEN_NOTIONAL` |
| 日新开最多笔数 | 20 | `RISK_MAX_DAILY_NEW_TRADES` |
| 硬止损回撤 | 35% | `RISK_HARD_DD` |
| 软降风险回撤 | 20% | `RISK_SOFT_DD` |
| 最小 edge | 200 bps (2%) | `LADDER_MIN_VIOLATION_BPS` |
| 最小簿深度 | $20 | `LADDER_MIN_DEPTH_USD` |
| 纸面模式 | true | `PAPER_MODE` |

---

## 上机（你的甲骨文 VPS）

```bash
# 登上 VPS 后
git clone https://github.com/jt8530-beep/poly.git
cd poly
git checkout arb-engine-v0

# 创建 Telegram bot:
# 1) 和 @BotFather 对话 -> /newbot -> 得 bot_token
# 2) 和你的 bot 对话 -> 发一条任意消息
# 3) 访问 https://api.telegram.org/bot<token>/getUpdates -> 取 chat.id

# 一键装好
bash scripts/deploy_oracle.sh

# 填 TG token + chat_id
nano ~/.config/arb-engine.env

# 重启
systemctl --user restart arb-engine

# 实时看日志
journalctl --user -u arb-engine -f
```

启动后你会在 Telegram 收到：
```
engine started, paper=true, min_edge=200bps
```
之后每当扫到信号，会推一条到你的 TG。

---

## 本地先跑一次看看

```bash
# 不需要 TG 也能跑，只是没有推送
python3 -m arb.main --once --paper
sqlite3 ledger.sqlite '.tables'
sqlite3 ledger.sqlite 'select count(*) from signals'
```

---

## 下一步

1. 纸面跑 **1 周** → 看 `signals` 表，评估：
   - 每天多少个信号
   - 信号的中位 edge bps
   - 这些违反平均能持续多久（数据不够就以每次扫描为粒度）
2. 达标（每天 > 3 条 edge > 200bps 信号）→ 实现 `executor.py`
3. 达不到 → 降低 `LADDER_MIN_VIOLATION_BPS` 或扩大扫描范围到策略 B（YES+NO<1）

---

## 明确没做的事

- ❌ 实盘下单（`executor.py` 是 stub，会直接抛 NotImplementedError）
- ❌ 策略 A（跨 Polymarket × 币安期权定价）—— 需要先把 C/B 跑稳
- ❌ 私钥管理 —— 实盘阶段再处理
- ❌ 新闻/情绪/方向性预测 —— 按你要求排除
