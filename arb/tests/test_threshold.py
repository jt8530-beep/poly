"""Quick sanity tests for the v1 threshold parser. Run with: python -m arb.tests.test_threshold"""
from __future__ import annotations
from arb.threshold_parser import extract_threshold


CASES = [
    # (question, expected)
    ("Will BTC be above $100,000 by Dec 31, 2026?",   100_000),
    ("Will BTC be above $100k by Dec 31, 2026?",      100_000),
    ("Will ETH reach $5k in 2026?",                    5_000),
    ("Will BTC hit 100k on July 4?",                   100_000),
    ("Will Fed cut 25 bps in July 2026?",              25),
    ("Will inflation exceed 5%?",                      5),
    ("Will SPX close above 6000 this year?",           6000),
    ("Russia captures Kostyantynivka by July 31",      None),   # pure time ladder, no threshold
    ("Will Taylor Swift be pregnant in 2025?",         None),
    ("BTC above $150k in December 2027?",              150_000),
    ("Will Dogecoin hit $1?",                          1),
    ("Will Nasdaq fall below 14,000 by Dec 2026?",     14_000),
]


def main():
    failed = 0
    for q, exp in CASES:
        got = extract_threshold(q)
        ok = (got == exp) or (exp is None and got is None) or (exp is not None and got is not None and abs(got - exp) < 1e-6)
        marker = "ok " if ok else "FAIL"
        print(f"{marker}  expect={exp!s:>10}  got={got!s:>10}  | {q}")
        if not ok:
            failed += 1
    print(f"\n{len(CASES)-failed}/{len(CASES)} passed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
