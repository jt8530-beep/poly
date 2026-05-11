"""
深入分析 0xe1d6...907c 在 Polymarket 上的实际玩法
关键问题:
1) 生涯 profit $797K  但最近118仓位 cashPnl -$7.6K, 赢家1/118 -> 钱到底哪来的?
2) 下注价格分布 -> 是不是在极低赔率挂单搏尾部?
3) 是 maker 还是 taker? 手续费/回扣贡献多少?
4) 单 trade 大小分布
"""
import json, glob, statistics, datetime as dt
from collections import Counter, defaultdict

acts=[]
for f in sorted(glob.glob('/tmp/poly/act*.json')):
    d=json.load(open(f))
    if isinstance(d,list): acts+=d
seen=set(); uniq=[]
for a in acts:
    k=(a.get('transactionHash'),a.get('timestamp'),a.get('asset'),a.get('side'),a.get('size'),a.get('price'),a.get('type'))
    if k in seen: continue
    seen.add(k); uniq.append(a)
acts=uniq
trades=[a for a in acts if a.get('type')=='TRADE']

# ---- price bucket
buckets=defaultdict(lambda:{'n':0,'usd':0.0,'buy_usd':0.0,'sell_usd':0.0})
for t in trades:
    p=t.get('price',0)
    # bucket by 0.05
    b=round(p//0.05 * 0.05, 2)
    buckets[b]['n']+=1
    buckets[b]['usd']+=t.get('usdcSize',0)
    if t.get('side')=='BUY':  buckets[b]['buy_usd']+=t.get('usdcSize',0)
    if t.get('side')=='SELL': buckets[b]['sell_usd']+=t.get('usdcSize',0)

print("=== price bucket distribution of trades (price = prob in contract) ===")
print(f"{'price bkt':>10} {'#trades':>8} {'volume $':>14} {'buy $':>14} {'sell $':>14}")
for b in sorted(buckets):
    r=buckets[b]
    print(f"{b:>10.2f} {r['n']:>8d} {r['usd']:>14,.0f} {r['buy_usd']:>14,.0f} {r['sell_usd']:>14,.0f}")

# ---- size distribution
sizes=sorted(t.get('usdcSize',0) for t in trades)
n=len(sizes)
print(f"\n=== trade size percentiles (n={n}) ===")
for q in (0.1,0.25,0.5,0.75,0.9,0.95,0.99):
    idx=int(q*n); print(f" p{int(q*100):>3}  ${sizes[idx]:,.2f}")
print(f" max   ${sizes[-1]:,.2f}")
print(f" sum   ${sum(sizes):,.2f}")

# ---- trades per market
per_mkt=defaultdict(list)
for t in trades:
    per_mkt[t.get('slug','?')].append(t)
print(f"\n=== markets touched: {len(per_mkt)} ===")
# high-cardinality market stats
print(f"{'slug':<55} {'#tr':>5} {'vol $':>11} {'avg px':>8} {'sides B/S':>10}")
for s,lst in sorted(per_mkt.items(), key=lambda x:-len(x[1]))[:15]:
    vol=sum(t.get('usdcSize',0) for t in lst)
    px=statistics.mean(t.get('price',0) for t in lst)
    bs=Counter(t.get('side','') for t in lst)
    print(f"{s[:55]:<55} {len(lst):>5d} {vol:>11,.0f} {px:>8.3f} {bs.get('BUY',0)}/{bs.get('SELL',0):>4}")

# ---- temporal: trades per minute in the 0.4-day window
minutes=defaultdict(int)
for t in trades:
    m=dt.datetime.utcfromtimestamp(t['timestamp']).replace(second=0)
    minutes[m]+=1
peaks=sorted(minutes.items(), key=lambda x:-x[1])[:10]
print(f"\n=== top 10 busiest minutes ===")
for m,c in peaks: print(f"  {m.isoformat()}Z   {c} trades")
print(f"median trades/minute (active minutes): {statistics.median(minutes.values())}")
print(f"active minutes: {len(minutes)}")
print(f"trades/minute avg over active minutes: {len(trades)/len(minutes):.1f}")
