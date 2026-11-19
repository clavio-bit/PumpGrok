"""onchain.deployer_history: creator wallet history from RPC (TOOL-002).

WHALE-only grant (R35); ``safety.score_report`` composes it in-process for
RUG. Counts come from paginated ``getSignaturesForAddress`` inside the
lookback window: ``tokens_seen`` is a signature count, not an adjudicated
token count, and ``rugs``/``worst_pnl_pct`` stay unwired until the
reputation book lands (Phase 4+).

The scan has a bounded budget. Hitting it sets ``truncated`` and the
safety check says ``at least N``. A full page of 100 is not reported as
a complete history.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import Field

from pumpgrok.core.errors import DataStale
from pumpgrok.core.ids import ambient_correlation
from pumpgrok.core.side_effect import SideEffect
from pumpgrok.core.tool import Tool, ToolInput, ToolOutput, ToolSpec, tool
from pumpgrok.tools._common import J5, SOLANA_RPC_SOURCE, MintStr, meta_now, utcnow
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError

# getSignaturesForAddress accepts at most 1000 signatures per call.
_PAGE_LIMIT = 1000
_MAX_PAGES = 5
_MAX_SIGNATURES = _PAGE_LIMIT * _MAX_PAGES
_DAY_S = 86_400


class DeployerHistoryIn(ToolInput):
    """History for ``creator`` over the trailing ``lookback_days``."""

    creator: MintStr
    lookback_days: int = Field(default=30, ge=1, le=90)


class DeployerHistoryOut(ToolOutput):
    """Signature-window history; adjudicated fields stay unwired."""

    tokens_seen: int = Field(ge=0)
    tokens_bought: int = Field(ge=0)
    rugs: int = Field(ge=0)
    worst_pnl_pct: Decimal | None = None
    is_known_bad: bool = False
    first_seen_at: datetime | None = None
    recent_signatures: list[str] = Field(default_factory=list)
    truncated: bool = False
    scan_limit: int = Field(default=_MAX_SIGNATURES, ge=1)


DEPLOYER_HISTORY_SPEC = ToolSpec(
    name="onchain.deployer_history",
    description=(
        "Creator wallet history from the RPC signature window; "
        "adjudicated rug fields are unwired pending the reputation book."
    ),
    input_model=DeployerHistoryIn,
    output_model=DeployerHistoryOut,
    side_effect=SideEffect.READ,
    timeout_s=20.0,
    retry=J5,
    rate_limit_key=SOLANA_RPC_SOURCE,
    backend="src/pumpgrok/tools/onchain/deployer_history.py via getSignaturesForAddress",
)


def _keep_entry(entry: object, cutoff: float) -> tuple[dict[str, Any] | None, bool]:
    """One signature row, and whether the scan has walked past the window."""
    if not isinstance(entry, dict) or not isinstance(entry.get("signature"), str):
        return None, False
    block_time = entry.get("blockTime")
    if isinstance(block_time, int) and block_time < cutoff:
        return None, True
    return entry, False


async def _one_page(rpc: RpcClient, creator: str, before: str | None) -> list[object]:
    options: dict[str, object] = {"limit": _PAGE_LIMIT}
    if before is not None:
        options["before"] = before
    try:
        sigs = await rpc.call("getSignaturesForAddress", [creator, options])
    except RpcError as exc:
        raise DataStale(f"onchain.deployer_history upstream failed creator={creator}") from exc
    if not isinstance(sigs, list):
        raise DataStale(f"onchain.deployer_history malformed creator={creator}")
    return sigs


async def _scan_signatures(
    rpc: RpcClient, creator: str, cutoff: float
) -> tuple[list[dict[str, Any]], bool]:
    """Pages until the lookback ends, the address is exhausted, or the budget hits."""
    kept: list[dict[str, Any]] = []
    before: str | None = None
    for _page in range(_MAX_PAGES):
        if len(kept) >= _MAX_SIGNATURES:
            return kept, True
        sigs = await _one_page(rpc, creator, before)
        if not sigs:
            return kept, False
        passed = False
        for entry in sigs:
            row, passed = _keep_entry(entry, cutoff)
            if passed:
                break
            if row is not None:
                kept.append(row)
        last = sigs[-1]
        cursor = last.get("signature") if isinstance(last, dict) else None
        exhausted = len(sigs) < _PAGE_LIMIT or not isinstance(cursor, str) or cursor == before
        if passed or exhausted:
            return kept, False
        before = cursor
    return kept, True


async def run_deployer_history(inp: DeployerHistoryIn, rpc: RpcClient) -> dict[str, Any]:
    """Scan the signature window and return a validated typed payload."""
    cutoff = utcnow(rpc.clock).timestamp() - inp.lookback_days * _DAY_S
    kept, truncated = await _scan_signatures(rpc, inp.creator, cutoff)
    stamps = [e["blockTime"] for e in kept if isinstance(e.get("blockTime"), int)]
    out = {
        "tokens_seen": len(kept),
        "tokens_bought": 0,
        "rugs": 0,
        "worst_pnl_pct": None,
        "is_known_bad": False,
        "first_seen_at": datetime.fromtimestamp(min(stamps), UTC) if stamps else None,
        "recent_signatures": [e["signature"] for e in kept[:10]],
        "truncated": truncated,
        "scan_limit": _MAX_SIGNATURES,
        "meta": meta_now(
            SOLANA_RPC_SOURCE,
            inp.correlation_id or (str(a) if (a := ambient_correlation()) else None),
            rpc.clock,
        ),
    }
    return DeployerHistoryOut.model_validate(out).model_dump()


def build_deployer_history_tool(rpc: RpcClient) -> Tool:
    """Bind ``onchain.deployer_history`` to one injected RPC client."""

    @tool(DEPLOYER_HISTORY_SPEC)
    async def _deployer_history(payload: DeployerHistoryIn) -> dict[str, Any]:
        return await run_deployer_history(payload, rpc)

    return _deployer_history
