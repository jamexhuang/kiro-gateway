"""
Region-endpoint feasibility spike.

Tests whether Kiro's q.{region}.amazonaws.com endpoint accepts requests using a
token whose account was registered in us-east-1.

Read-only — only calls ListAvailableModels (no inference, no quota burn).

Usage:
    python scripts/region_spike.py [--account-index 0]

Reports per-region: status code, latency, error reason, model count.
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

# Make project importable when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiro.account_manager import AccountManager  # noqa: E402
from kiro.auth import AuthType  # noqa: E402

# Per https://kiro.dev/docs/privacy-and-security/data-protection/
SUPPORTED_REGIONS: List[str] = [
    # US
    "us-east-1",
    "us-east-2",
    "us-west-2",
    # Europe
    "eu-central-1",
    "eu-west-1",
    "eu-west-3",
    "eu-north-1",
    "eu-south-1",
    "eu-south-2",
]


async def probe_region(
    region: str,
    token: str,
    profile_arn: Optional[str],
    auth_type: AuthType,
) -> Dict[str, Any]:
    url = f"https://q.{region}.amazonaws.com/ListAvailableModels"
    params: Dict[str, str] = {"origin": "AI_EDITOR"}
    if auth_type == AuthType.KIRO_DESKTOP and profile_arn:
        params["profileArn"] = profile_arn

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                params=params,
            )
            elapsed_ms = (time.monotonic() - started) * 1000
            body_excerpt: str = ""
            model_count: Optional[int] = None
            try:
                data = resp.json()
                if isinstance(data, dict) and "models" in data:
                    model_count = len(data["models"])
                body_excerpt = json.dumps(data)[:200]
            except Exception:
                body_excerpt = resp.text[:200]
            return {
                "region": region,
                "status": resp.status_code,
                "elapsed_ms": round(elapsed_ms, 1),
                "model_count": model_count,
                "body_excerpt": body_excerpt,
                "error": None,
            }
    except httpx.HTTPError as e:
        return {
            "region": region,
            "status": None,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
            "model_count": None,
            "body_excerpt": "",
            "error": f"{type(e).__name__}: {e}",
        }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--account-index",
        type=int,
        default=0,
        help="Which account from credentials.json to use (0-based).",
    )
    args = ap.parse_args()

    creds_file = Path("credentials.json")
    state_file = Path("state.json")
    if not creds_file.exists():
        print(f"ERROR: {creds_file} not found in cwd.")
        return 1

    manager = AccountManager(
        credentials_file=str(creds_file),
        state_file=str(state_file),
    )
    await manager.load_credentials()
    await manager.load_state()

    account_ids = list(manager._accounts.keys())
    if not account_ids:
        print("ERROR: no accounts loaded.")
        return 1
    if args.account_index >= len(account_ids):
        print(f"ERROR: --account-index {args.account_index} out of range (have {len(account_ids)}).")
        return 1

    target_id = account_ids[args.account_index]
    print(f"Spike account: {target_id}\n")

    # Initialize this account (lazy)
    ok = await manager._initialize_account(target_id)
    if not ok:
        print("ERROR: failed to initialize account.")
        return 1
    account = manager._accounts[target_id]
    auth = account.auth_manager
    if auth is None:
        print("ERROR: auth_manager not ready.")
        return 1

    print(f"Auth type: {auth.auth_type.value}")
    print(f"Native q_host: {auth.q_host}")
    print(f"Profile ARN: {auth.profile_arn or '(none)'}")
    print()

    token = await auth.get_access_token()
    print(f"Token fingerprint: ...{token[-8:]} (len={len(token)})\n")

    results: List[Dict[str, Any]] = []
    for region in SUPPORTED_REGIONS:
        print(f"  probing {region:<14} ... ", end="", flush=True)
        r = await probe_region(region, token, auth.profile_arn, auth.auth_type)
        results.append(r)
        if r["status"] == 200:
            print(f"OK    {r['elapsed_ms']:>6.0f}ms  models={r['model_count']}")
        elif r["error"]:
            print(f"NET   {r['elapsed_ms']:>6.0f}ms  {r['error']}")
        else:
            print(f"HTTP{r['status']} {r['elapsed_ms']:>5.0f}ms  body={r['body_excerpt'][:120]}")

    print("\n=== Summary ===")
    success = [r for r in results if r["status"] == 200]
    rejected = [r for r in results if r["status"] and r["status"] != 200]
    network_err = [r for r in results if r["error"]]

    print(f"  accepted   : {[r['region'] for r in success]}")
    print(f"  rejected   : {[(r['region'], r['status']) for r in rejected]}")
    print(f"  net errors : {[(r['region'], r['error']) for r in network_err]}")

    # Persist full payload for later inspection
    out = Path("debug_logs") / "region_spike_result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {
            "account": target_id,
            "auth_type": auth.auth_type.value,
            "native_q_host": auth.q_host,
            "results": results,
        },
        indent=2,
    ))
    print(f"\nFull dump → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
