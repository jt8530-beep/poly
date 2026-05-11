# poly

Polymarket 相关的分析与回测工作区。与 QuantConnect / IBIT 项目完全独立。

## 当前内容

- [`honest_backtest/`](./honest_backtest/) —— 对"1200 美元 49 天变 79.7 万美元"Polymarket 量化帖子的事实核查
  - 钱包取证：拉 Polymarket 官方 API，还原真实账户状态（结论：当前净值 $4.41，刷量账户）
  - 诚实版回测：把帖子图中 EV / Kelly / Bayes / Maker-Taker 公式全部实现，用 Monte Carlo 验证真实收益分布
  - 详见 [honest_backtest/README.md](./honest_backtest/README.md)

## 后续计划

待补充：
- 实时 Polymarket 市价 + 订单簿抓取
- 自研信号 / 赔率偏差检测
- 回测框架扩展到真实历史数据
