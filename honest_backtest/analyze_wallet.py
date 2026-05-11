import json, glob, statistics, datetime as dt
from collections import Counter, defaultdict

# --- load all activity
acts = []
for f in sorted(glob.glob('/tmp/poly/act*.json')):
    acts += json.load(open(f))

# de-dup
seen=set(); uniq=[]
for a in acts:
    k=(a.get('transactionHash'),a.get('timestamp'),a.get('asset'),a.get('side'),a.get('size'),a.get('price'),a.get('type'))
    if k in seen: continue
    seen.add(k); uniq.append(a)
acts = uniq
print(f"total activity rows (deduped): {len(acts)}")

types = Counter(a.get('type','') for a in acts)
print("by type:", dict(types))

trades = [a for a in acts if a.get('type')=='TRADE']
print(f"\ntrades: {len(trades)}")

if trades:
    ts = [a['timestamp'] for a in trades]
    t_min, t_max = min(ts), max(ts)
    d_min = dt.datetime.utcfromtimestamp(t_min)
    d_max = dt.datetime.utcfromtimestamp(t_max)
    span_days = (t_max-t_min)/86400
    print(f"trading window: {d_min.isoformat()}Z  ->  {d_max.isoformat()}Z   ({span_days:.1f} days)")
    vols = [a.get('usdcSize',0) for a in trades]
    print(f"total USDC volume (trades): ${sum(vols):,.2f}")
    print(f"avg trade: ${statistics.mean(vols):.2f}   median: ${statistics.median(vols):.2f}")
    sides = Counter(a.get('side','') for a in trades)
    print("sides:", dict(sides))

rebates = [a for a in acts if a.get('type')=='MAKER_REBATE']
print(f"\nMAKER_REBATE events: {len(rebates)}   total rebate volume: ${sum(a.get('usdcSize',0) for a in rebates):,.2f}")

pos = json.load(open('/tmp/poly/pos1.json'))
tot_cash_pnl = sum(p.get('cashPnl',0) for p in pos)
tot_realized = sum(p.get('realizedPnl',0) for p in pos)
tot_curval   = sum(p.get('currentValue',0) for p in pos)
tot_initval  = sum(p.get('initialValue',0) for p in pos)
tot_bought   = sum(p.get('totalBought',0) for p in pos)
print(f"\n--- positions ({len(pos)}) ---")
print(f"sum cashPnl (unrealized at snapshot): ${tot_cash_pnl:,.2f}")
print(f"sum realizedPnl:                      ${tot_realized:,.2f}")
print(f"sum currentValue:                     ${tot_curval:,.2f}")
print(f"sum initialValue:                     ${tot_initval:,.2f}")
print(f"sum totalBought:                      ${tot_bought:,.2f}")

winners = [p for p in pos if p.get('cashPnl',0) > 0]
losers  = [p for p in pos if p.get('cashPnl',0) <= 0]
print(f"winners: {len(winners)}   losers: {len(losers)}")
if pos:
    pct = sorted(p.get('percentPnl',0) for p in pos)
    print(f"%PnL  min:{pct[0]:.1f}%  median:{pct[len(pct)//2]:.1f}%  max:{pct[-1]:.1f}%")

slugs = Counter(a.get('slug','') for a in trades)
print("\nTop 10 markets by trade count:")
for s,c in slugs.most_common(10): print(f"  {c:5d}  {s}")

btc5m = sum(1 for s in (a.get('slug','') for a in trades) if s.startswith('btc-updown-5m'))
print(f"\nBTC 5-min updown share: {btc5m}/{len(trades)} = {100*btc5m/max(1,len(trades)):.1f}%")

daily = defaultdict(float)
for t in trades:
    d = dt.datetime.utcfromtimestamp(t['timestamp']).date()
    daily[d] += t.get('usdcSize',0)
print("\nLast 10 trading days by USDC volume:")
for d in sorted(daily.keys())[-10:]:
    print(f"  {d}  ${daily[d]:,.2f}")
