"""
Live executor - intentionally a STUB.

Polymarket CLOB orders are signed with your Polygon private key using EIP-712.
We will implement this AFTER the ladder strategy has produced >=1 week of
paper data that validates edge and execution assumptions.

When you are ready to go live:
  1. Install the official SDK:   pip install py_clob_client
  2. Uncomment and complete the functions below
  3. Flip PAPER_MODE=false in env

Keeping this disabled is a deliberate safety measure.
"""
from __future__ import annotations
import logging

log = logging.getLogger("executor")


def submit_paired_orders(cfg, signal, notional_usd: float):
    """
    Would:
      - send a SELL (taker) on leg A to hit the overpriced bid
      - send a BUY  (taker) on leg B to take the underpriced ask
      - or, in paper/limit mode: post both as maker IOC
      - on ANY failure, reverse the successfully filled leg
    """
    raise NotImplementedError(
        "Live execution disabled in v0. Run in PAPER_MODE=true until "
        "arb-engine-v0 ships >=1 week of validated paper fills."
    )
