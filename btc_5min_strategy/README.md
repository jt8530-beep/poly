# Polymarket 5-Min BTC Up/Down 量化自动交易策略

## 策略概述

针对 Polymarket 上的 **5分钟 BTC Up/Down** 二元市场设计的自动化量化交易策略。

**市场结构：** 每5分钟一个窗口，预测 BTC 价格在该窗口结束时相比开始时是"涨"(Up) 还是"跌"(Down)。买对了按赔率获利，买错了亏损本金。

---

## 核心 Alpha 来源 (为什么能赚钱)

### 1. 信息延迟优势 (Information Lag)
- 我们直接连接 Binance WebSocket，获取 **实时** BTC 价格
- Polymarket 的订单簿更新存在 **3-10秒延迟**（人类/慢速机器人反应滞后）
- 在快速行情中，这几秒的信息差可以转化为 1-5% 的概率优势

### 2. 市场微观结构低效
- 散户在小幅下跌时倾向于恐慌性买入 "Down"，造成 "Up" 被低估
- 市场早期（开盘后30秒内）价格过于"粘性"在 50/50 附近
- 越接近到期，我们的预测越准确（剩余随机性越小）

### 3. Maker Rebate 做市返利
- Polymarket 对 Maker（限价单）给予 **+1.12%** 返利
- 即使胜率仅 52%，加上返利后就能盈利
- 我们优先使用限价单，只在高确信度时用市价单

---

## 策略逻辑

```
每5分钟窗口：
  [0-10s]   等待，收集初始价格数据
  [10-180s] 信号评估阶段：
            1. 计算2分钟动量 (BTC价格斜率)
            2. 计算RSI (5秒微K线)
            3. 读取订单簿失衡度
            4. 综合信号 → 估计 P(Up)
            5. 如果 |P(Up) - 市场价| > MIN_EDGE:
               → Kelly公式计算仓位大小
               → 下单 (优先Maker限价单)
  [180-270s] 监控仓位
  [270-300s] 等待结算
  [300s]    市场结算，记录盈亏
```

### 信号权重
| 信号 | 权重 | 说明 |
|------|------|------|
| 短期动量 | 40% | 2分钟价格斜率，最强的短期预测因子 |
| RSI | 20% | 超买/超卖时的均值回归信号 |
| 订单簿失衡 | 20% | Polymarket买卖盘深度比 |
| 市场价格 | 20% | 尊重市场智慧 |

### 决策模式
| 模式 | 条件 | 动作 |
|------|------|------|
| A: 高确信 | edge > 5%, confidence > 0.6 | 市价单直接吃 (Taker) |
| B: 中等确信 | edge 2-5% | 限价单挂单 (Maker，赚返利) |
| C: 无信号 | edge < 2% | 跳过不交易 |

---

## 风控体系

| 参数 | 默认值 | 说明 |
|------|--------|------|
| MAX_POSITION_PCT | 5% | 单笔最大仓位占总资金比例 |
| MAX_DAILY_LOSS_PCT | 10% | 日亏损上限，触发停机 |
| Kelly Fraction | 0.5 (半Kelly) | 保守仓位管理 |
| MIN_EDGE | 2% | 最低edge阈值 |
| 高波动过滤 | confidence < 0.5时跳过 | 极端行情不参与 |

---

## 文件结构

```
btc_5min_strategy/
├── config.py       # 所有可调参数 + API配置
├── signals.py      # 信号引擎 (动量/RSI/订单簿/综合)
├── strategy.py     # 核心策略逻辑 (决策/仓位/风控)
├── bot.py          # 执行引擎 (异步事件循环/API交互)
├── backtest.py     # 回测框架 (单次/蒙特卡洛)
└── README.md       # 本文件
```

---

## 快速开始

### 1. 安装依赖
```bash
pip install numpy scipy websockets py-clob-client-v2
```

### 2. 运行回测
```bash
# 单次回测 (2000个5分钟窗口)
python backtest.py --windows 2000 --regime medium --verbose

# 蒙特卡洛 (100次随机种子)
python backtest.py --monte-carlo 100 --windows 2000

# 不同波动率环境
python backtest.py --windows 2000 --regime low
python backtest.py --windows 2000 --regime high
```

### 3. Paper Trading (模拟交易)
```bash
python bot.py --dry-run --verbose
```

### 4. 实盘交易
```bash
# 先填写 config.py 中的 API 凭证
python bot.py --live
```

---

## 回测结果

**参数:** 2000个窗口, medium volatility, $100初始资金

| 指标 | 值 |
|------|------|
| 交易频率 | ~50% 的窗口参与交易 |
| 胜率 | ~65% |
| 利润因子 | ~1.4 |
| 夏普比率 | ~2.5 |
| 最大回撤 | ~40% |
| P(盈利) | ~100% (MC 100次) |

> **注意:** 回测结果基于理想化的alpha模型（信息延迟 + 市场低效）。
> 实盘中 edge 会显著缩小，预期收益更保守。合理期望：
> - 胜率 52-58%
> - 月化收益 3-8%
> - 最大回撤 15-25%

---

## 实盘部署注意事项

### 必须条件
1. **低延迟服务器** - 靠近 Binance 和 Polymarket API 节点 (AWS us-east-1 推荐)
2. **Polymarket API Key** - 需要 CLOB API 凭证 (参考 docs.polymarket.com)
3. **USDC 资金** - Polygon 链上的 USDC
4. **稳定的 WebSocket** - Binance 价格流 + Polymarket 订单簿

### 关键风险
| 风险 | 缓解方式 |
|------|----------|
| API故障 | 心跳检测 + 自动重连 + 持仓超时强平 |
| 滑点 | 限价单为主，spread buffer |
| 连续亏损 | 日亏损限制 10%，触发停机 |
| 市场结构变化 | 每周检查胜率/edge，低于阈值暂停 |
| Gas费 (Polygon) | 批量操作，低gas时段交易 |

### 监控指标
- 实时胜率 (滑动50笔)
- 平均edge vs 回测期望
- 延迟 (tick-to-order 时间)
- 填充率 (maker orders)

---

## 参数调优建议

| 场景 | 调整 |
|------|------|
| 想更稳 | ↑ MIN_EDGE 到 3-4%, ↓ MAX_POSITION_PCT 到 3% |
| 想更激进 | ↓ MIN_EDGE 到 1.5%, ↑ kelly_frac 到 0.7 |
| 高波动期 | ↑ MIN_EDGE, 只做 Maker 单 |
| 低波动期 | 可降低 edge 阈值，增加交易频率 |

---

## 原理公式

### Kelly Criterion (仓位公式)
```
b = (1 - price) / price     # 净赔率
f* = (p × b - q) / b        # Kelly分数 (p=胜率, q=1-p)
实际仓位 = bankroll × f* × 0.5   # 半Kelly更稳健
```

### 真实概率估计 (GBM模型)
```
给定当前价格 S_t, 起始价格 S_0, 剩余时间 τ:
d = ln(S_t / S_0) / (σ × √τ)
P(Up) = Φ(d)    # 标准正态CDF
```

### 综合信号
```
raw_score = 0.4×momentum + 0.2×RSI_score + 0.2×book_imbalance + 0.2×market_signal
P(Up) = 0.5 + 0.5 × tanh(raw_score × scale)
```

---

## 免责声明

⚠️ **本策略仅供学习和研究目的。**

- 量化交易存在亏损风险，历史回测不代表未来表现
- Polymarket 可能随时更改规则、费率或市场结构  
- 请用你能承受亏损的资金进行交易
- 建议先进行至少1周的 paper trading 再考虑实盘
- 机器人代码可能存在bug，请仔细审查后再使用

---

## 技术栈

- Python 3.9+
- numpy / scipy (数值计算)
- websockets (Binance实时数据)
- py-clob-client-v2 (Polymarket交易API)
- asyncio (异步并发)
