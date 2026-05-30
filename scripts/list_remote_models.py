# -*- coding: utf-8 -*-

"""
List available models from Kiro remote API (/ListAvailableModels).

Useful for checking which models your Kiro account currently has access to,
including token limits (maxInputTokens, maxOutputTokens).

Usage:
    python list_remote_models.py

Requirements:
    - Valid credentials configured in .env (KIRO_CREDS_FILE, REFRESH_TOKEN, or KIRO_CLI_DB_FILE)

Output:
    - Human-readable table of models with token limits
    - JSON dump of the raw Kiro API response
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Dict, List

import httpx
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

from kiro.auth import KiroAuthManager
from kiro.config import KIRO_CREDS_FILE, PROFILE_ARN, REGION


async def fetch_remote_models() -> Dict[str, Any]:
    """
    Fetch the raw ListAvailableModels response from Kiro API.

    Returns:
        The parsed JSON response from Kiro API.

    Raises:
        httpx.HTTPStatusError: If the API returns a non-2xx status code.
    """
    auth = KiroAuthManager(
        creds_file=KIRO_CREDS_FILE,
        region=REGION,
        profile_arn=PROFILE_ARN,
    )

    token = await auth.get_access_token()
    logger.info(f"Token acquired (length: {len(token)})")
    logger.info(f"Region: {auth.region}, Q host: {auth.q_host}")

    url = f"{auth.q_host}/ListAvailableModels"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    params: Dict[str, str] = {"origin": "AI_EDITOR"}
    if auth.profile_arn:
        params["profileArn"] = auth.profile_arn

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()


def print_models_table(models: List[Dict[str, Any]]) -> None:
    """
    Print a human-readable table of models with token limits.

    Args:
        models: List of model dicts from Kiro API.
    """
    print(f"\n=== Kiro API returned {len(models)} models ===\n")
    print(f"  {'#':>2}  {'modelId':<22}  {'displayName':<22}  {'maxInput':>10}  {'maxOutput':>10}")
    print(f"  {'-'*2}  {'-'*22}  {'-'*22}  {'-'*10}  {'-'*10}")
    for i, model in enumerate(models, 1):
        model_id = model.get("modelId", "?")
        display = model.get("displayName", model.get("modelName", "?"))
        limits = model.get("tokenLimits", {}) or {}
        max_input = limits.get("maxInputTokens", "?")
        max_output = limits.get("maxOutputTokens", "?")
        print(f"  {i:>2}  {model_id:<22}  {display:<22}  {max_input:>10}  {max_output:>10}")
    print()


async def main() -> int:
    """
    Main entry point.

    Returns:
        Exit code (0 on success, non-zero on failure).
    """
    try:
        data = await fetch_remote_models()
    except httpx.HTTPStatusError as exc:
        logger.error(f"Kiro API returned HTTP {exc.response.status_code}: {exc.response.text}")
        return 1
    except httpx.RequestError as exc:
        logger.error(f"Network error contacting Kiro API: {exc}")
        return 1

    models = data.get("models", [])
    print_models_table(models)

    print("Raw JSON response:")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
