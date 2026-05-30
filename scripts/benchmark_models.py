
import asyncio
import json
import os
import time
import sys
import uuid
from typing import List, Dict, Any

import httpx
from loguru import logger
from kiro.auth import KiroAuthManager
from kiro.config import KIRO_CREDS_FILE, PROFILE_ARN, REGION
from kiro.tokenizer import count_tokens

# Configure logger to be less verbose for cleaner benchmark output
logger.remove()
logger.add(sys.stderr, level="INFO")

async def benchmark_model(model_id: str, prompt: str):
    print(f"\n--- Benchmarking {model_id} ---")
    
    auth = KiroAuthManager(
        creds_file=KIRO_CREDS_FILE,
        region=REGION,
        profile_arn=PROFILE_ARN,
    )
    
    token = await auth.get_access_token()
    url = f"{auth.api_host}/generateAssistantResponse"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "KiroIDE-0.7.45",
    }
    
    payload = {
        "conversationState": {
            "agentContinuationId": str(uuid.uuid4()),
            "agentTaskType": "vibe",
            "chatTriggerType": "MANUAL",
            "conversationId": str(uuid.uuid4()),
            "currentMessage": {
                "userInputMessage": {
                    "content": prompt,
                    "modelId": model_id,
                    "origin": "AI_EDITOR",
                }
            },
            "history": []
        }
    }
    
    if auth.profile_arn and auth.auth_type.value != "aws_sso_oidc":
        payload["profileArn"] = auth.profile_arn

    start_time = time.time()
    first_token_time = None
    total_content = ""
    chunk_count = 0
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    print(f"Error: {response.status_code}")
                    return None
                
                async for chunk in response.aiter_bytes():
                    if not first_token_time:
                        first_token_time = time.time()
                    
                    # Very rough parsing of Kiro's event stream to extract text content
                    chunk_str = chunk.decode('utf-8', errors='ignore')
                    
                    if '"content":"' in chunk_str:
                        parts = chunk_str.split('"content":"')
                        for part in parts[1:]:
                            content_val = part.split('"', 1)[0]
                            total_content += content_val
                    
                    chunk_count += 1
                    
    except Exception as e:
        print(f"Benchmark failed: {e}")
        return None

    end_time = time.time()
    total_duration = end_time - start_time
    ttft = (first_token_time - start_time) if first_token_time else 0
    generation_time = end_time - first_token_time if first_token_time else 0
    
    token_count = count_tokens(total_content)
    tps = token_count / generation_time if generation_time > 0 else 0
    
    print(f"  Time to First Token (TTFT): {ttft:.2f}s")
    print(f"  Total Duration: {total_duration:.2f}s")
    print(f"  Estimated Tokens: {token_count}")
    print(f"  Speed (TPS): {tps:.2f} tokens/s")
    
    return {
        "model": model_id,
        "ttft": ttft,
        "tps": tps,
        "tokens": token_count,
        "duration": total_duration
    }

async def run_benchmarks():
    prompt = "Write a long, detailed story about a space explorer discovering a sentient nebula. At least 500 words."
    
    models = ["claude-opus-4.7", "claude-opus-4.6"]
    results = []
    
    for model in models:
        res = await benchmark_model(model, prompt)
        if res:
            results.append(res)
            
    if len(results) == 2:
        print("\n=== Comparison Results ===")
        m1, m2 = results[0], results[1]
        tps_diff = ((m1['tps'] - m2['tps']) / m2['tps']) * 100 if m2['tps'] > 0 else 0
        ttft_diff = ((m1['ttft'] - m2['ttft']) / m2['ttft']) * 100 if m2['ttft'] > 0 else 0
        
        print(f"Speed (TPS): {m1['model']} is {abs(tps_diff):.1f}% {'faster' if tps_diff > 0 else 'slower'} than {m2['model']}")
        print(f"Latency (TTFT): {m1['model']} is {abs(ttft_diff):.1f}% {'higher' if ttft_diff > 0 else 'lower'} than {m2['model']}")

if __name__ == "__main__":
    asyncio.run(run_benchmarks())
