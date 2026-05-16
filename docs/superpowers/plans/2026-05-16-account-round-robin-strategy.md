# Account Round-Robin Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `ACCOUNT_STRATEGY` toggle so operators can switch the multi-account selection algorithm from the current `sticky` failover to `round_robin` (rotate one account per request) while keeping all Circuit Breaker / failover behaviour intact.

**Architecture:** Introduce a single config flag `ACCOUNT_STRATEGY` (default `sticky`, alt `round_robin`). The change is isolated to `kiro/account_manager.py`: `get_next_account()` computes the starting index based on strategy, and `report_success()` only updates the global sticky index in `sticky` mode. State persistence schema is unchanged (`current_account_index` is reused as the "last served" cursor for round-robin too). Single-account fast path is untouched.

**Tech Stack:** Python 3.10+, FastAPI, asyncio, pytest, pytest-asyncio.

---

## File Structure

- `kiro/config.py` — add `ACCOUNT_STRATEGY` env var parsing + validation (one new constant, ~6 lines).
- `kiro/account_manager.py` — modify `get_next_account()` (start-index computation), modify `report_success()` (skip sticky update in round_robin mode), import the new constant. No schema change to state.json.
- `tests/unit/test_account_manager.py` — add a new test class `TestAccountManagerRoundRobinStrategy` with focused tests.
- `.env.example` — document the new toggle.
- `AGENTS.md` — one-line entry in the env vars reference table (if such table exists; otherwise skip).

---

## Task 1: Add `ACCOUNT_STRATEGY` config constant

**Files:**
- Modify: `kiro/config.py` (insert after the Circuit Breaker block, around line 598)

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_account_manager.py` (at the end of the file):

```python
class TestAccountStrategyConfig:
    """Tests for ACCOUNT_STRATEGY env var parsing."""

    def test_default_strategy_is_sticky(self, monkeypatch):
        """Default value when env var unset is 'sticky'."""
        monkeypatch.delenv("ACCOUNT_STRATEGY", raising=False)
        import importlib
        import kiro.config
        importlib.reload(kiro.config)
        assert kiro.config.ACCOUNT_STRATEGY == "sticky"

    def test_round_robin_strategy_parsed(self, monkeypatch):
        """'round_robin' is accepted (case-insensitive)."""
        monkeypatch.setenv("ACCOUNT_STRATEGY", "Round_Robin")
        import importlib
        import kiro.config
        importlib.reload(kiro.config)
        assert kiro.config.ACCOUNT_STRATEGY == "round_robin"

    def test_invalid_strategy_falls_back_to_sticky(self, monkeypatch):
        """Unknown values fall back to 'sticky' (defensive)."""
        monkeypatch.setenv("ACCOUNT_STRATEGY", "garbage")
        import importlib
        import kiro.config
        importlib.reload(kiro.config)
        assert kiro.config.ACCOUNT_STRATEGY == "sticky"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_account_manager.py::TestAccountStrategyConfig -v`
Expected: FAIL with `AttributeError: module 'kiro.config' has no attribute 'ACCOUNT_STRATEGY'`

- [ ] **Step 3: Add the constant to `kiro/config.py`**

Insert after line 598 (`ACCOUNT_PROBABILISTIC_RETRY_CHANCE` definition), before the `# Account Cache Settings` divider:

```python
# Account selection strategy for multi-account mode (ignored when only 1 account)
# Options:
#   - "sticky"      : (default) Keep using the last successful account until it fails.
#                     Best for cache hit rate and minimising token refresh churn.
#   - "round_robin" : Rotate to the next account on every request to spread load.
#                     Failover loop (Circuit Breaker, exclude_accounts) still applies.
# Unknown values fall back to "sticky".
_ACCOUNT_STRATEGY_RAW: str = os.getenv("ACCOUNT_STRATEGY", "sticky").lower().strip()
ACCOUNT_STRATEGY: str = _ACCOUNT_STRATEGY_RAW if _ACCOUNT_STRATEGY_RAW in ("sticky", "round_robin") else "sticky"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_account_manager.py::TestAccountStrategyConfig -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
rtk git add kiro/config.py tests/unit/test_account_manager.py
rtk git commit -m "feat(config): add ACCOUNT_STRATEGY toggle (sticky|round_robin)"
```

---

## Task 2: Wire round-robin into `get_next_account()`

**Files:**
- Modify: `kiro/account_manager.py:51-62` (import block) and `kiro/account_manager.py:706-717` (multi-account start-index logic)

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_account_manager.py` (before `class TestAccountStrategyConfig`):

```python
class TestAccountManagerRoundRobinStrategy:
    """
    Tests for round_robin strategy in get_next_account().

    Verifies the selection cursor advances by one on every fresh
    get_next_account() call, while preserving failover semantics
    when exclude_accounts is non-empty.
    """

    async def _build_three_account_manager(self, tmp_path, mock_list_models_response):
        """Helper: build a manager with 3 initialised accounts."""
        creds_entries = []
        for i in range(3):
            jf = tmp_path / f"acct_{i}.json"
            jf.write_text(json.dumps({
                "refreshToken": f"token_{i}",
                "accessToken": f"access_{i}",
                "expiresAt": "2099-01-01T00:00:00.000Z",
            }))
            creds_entries.append({"type": "json", "path": str(jf), "enabled": True})

        creds_file = tmp_path / "credentials.json"
        creds_file.write_text(json.dumps(creds_entries))

        manager = AccountManager(
            credentials_file=str(creds_file),
            state_file=str(tmp_path / "state.json"),
        )
        await manager.load_credentials()

        with patch('kiro.account_manager.KiroHttpClient') as mock_http_class:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_list_models_response
            mock_client.request_with_retry = AsyncMock(return_value=mock_response)
            mock_client.close = AsyncMock()
            mock_http_class.return_value = mock_client

            for acct_id in list(manager._accounts.keys()):
                await manager._initialize_account(acct_id)

        return manager

    @pytest.mark.asyncio
    async def test_round_robin_advances_cursor_each_call(
        self, tmp_path, mock_list_models_response, monkeypatch,
    ):
        """Three consecutive get_next_account calls return three different accounts."""
        print("\n=== Test: round_robin advances cursor each call ===")
        monkeypatch.setattr("kiro.account_manager.ACCOUNT_STRATEGY", "round_robin")

        manager = await self._build_three_account_manager(tmp_path, mock_list_models_response)
        ids = list(manager._accounts.keys())
        manager._current_account_index = 0

        a = await manager.get_next_account("claude-opus-4.5")
        b = await manager.get_next_account("claude-opus-4.5")
        c = await manager.get_next_account("claude-opus-4.5")

        picked = [a.id, b.id, c.id]
        print(f"Picked: {picked}")
        assert picked == [ids[1], ids[2], ids[0]]

    @pytest.mark.asyncio
    async def test_sticky_keeps_same_account(
        self, tmp_path, mock_list_models_response, monkeypatch,
    ):
        """In sticky mode, three calls without failure return the same account."""
        print("\n=== Test: sticky mode keeps same account ===")
        monkeypatch.setattr("kiro.account_manager.ACCOUNT_STRATEGY", "sticky")

        manager = await self._build_three_account_manager(tmp_path, mock_list_models_response)
        ids = list(manager._accounts.keys())
        manager._current_account_index = 0

        a = await manager.get_next_account("claude-opus-4.5")
        b = await manager.get_next_account("claude-opus-4.5")
        c = await manager.get_next_account("claude-opus-4.5")

        print(f"Picked: {[a.id, b.id, c.id]}")
        assert a.id == b.id == c.id == ids[0]

    @pytest.mark.asyncio
    async def test_round_robin_failover_walks_remaining(
        self, tmp_path, mock_list_models_response, monkeypatch,
    ):
        """When exclude_accounts is given (mid-failover), cursor is NOT advanced;
        the loop must walk the remaining accounts starting from cursor."""
        print("\n=== Test: round_robin honours exclude_accounts ===")
        monkeypatch.setattr("kiro.account_manager.ACCOUNT_STRATEGY", "round_robin")

        manager = await self._build_three_account_manager(tmp_path, mock_list_models_response)
        ids = list(manager._accounts.keys())
        manager._current_account_index = 0

        first = await manager.get_next_account("claude-opus-4.5")
        # Simulate failover: caller adds first.id to exclude and asks again
        second = await manager.get_next_account(
            "claude-opus-4.5",
            exclude_accounts={first.id},
        )

        print(f"First={first.id}, second={second.id}")
        assert first.id == ids[1]
        assert second.id != first.id
        assert second.id in {ids[0], ids[2]}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_account_manager.py::TestAccountManagerRoundRobinStrategy -v`
Expected: `test_round_robin_advances_cursor_each_call` FAILS (all three calls return `ids[0]` in current sticky implementation). `test_sticky_keeps_same_account` PASSES (already true). `test_round_robin_failover_walks_remaining` will FAIL with same root cause.

- [ ] **Step 3: Add the import**

In `kiro/account_manager.py`, modify the `from kiro.config import (...)` block (lines 51-62) to add `ACCOUNT_STRATEGY`:

```python
from kiro.config import (
    HIDDEN_MODELS,
    MODEL_ALIASES,
    MODEL_FAMILY_ALIASES,
    HIDDEN_FROM_LIST,
    ACCOUNT_RECOVERY_TIMEOUT,
    ACCOUNT_MAX_BACKOFF_MULTIPLIER,
    ACCOUNT_PROBABILISTIC_RETRY_CHANCE,
    ACCOUNT_CACHE_TTL,
    ACCOUNT_STRATEGY,
    STATE_SAVE_INTERVAL_SECONDS,
    FALLBACK_MODELS,
)
```

- [ ] **Step 4: Modify `get_next_account()` multi-account branch**

In `kiro/account_manager.py`, locate the block starting at line 706 (`# Multi-account logic: GLOBAL sticky`) and replace lines 706-713 with:

```python
            # Multi-account logic
            normalized_model = normalize_model_name(model)

            all_account_ids = list(self._accounts.keys())

            # Compute starting index based on strategy.
            # round_robin: advance cursor on every *fresh* call (no exclude set).
            #              During an in-flight failover loop (exclude_accounts present),
            #              we keep the existing cursor so the walk visits the remaining
            #              accounts in the same rotation order.
            # sticky:     always start from the current cursor.
            if (
                ACCOUNT_STRATEGY == "round_robin"
                and not exclude_accounts
                and len(all_account_ids) > 1
            ):
                self._current_account_index = (self._current_account_index + 1) % len(all_account_ids)
                self._dirty = True

            start_index = self._current_account_index
```

- [ ] **Step 5: Run round-robin tests to verify they pass**

Run: `pytest tests/unit/test_account_manager.py::TestAccountManagerRoundRobinStrategy -v`
Expected: 3 passed.

- [ ] **Step 6: Run full account_manager test file to catch regressions**

Run: `pytest tests/unit/test_account_manager.py -v`
Expected: all tests pass (no regressions in the existing 70+ tests).

- [ ] **Step 7: Commit**

```bash
rtk git add kiro/account_manager.py tests/unit/test_account_manager.py
rtk git commit -m "feat(account-system): round_robin strategy in get_next_account"
```

---

## Task 3: Prevent `report_success()` from clobbering the round-robin cursor

**Files:**
- Modify: `kiro/account_manager.py:794-802`

- [ ] **Step 1: Write the failing test**

Add to the same `TestAccountManagerRoundRobinStrategy` class:

```python
    @pytest.mark.asyncio
    async def test_round_robin_report_success_does_not_reset_cursor(
        self, tmp_path, mock_list_models_response, monkeypatch,
    ):
        """In round_robin mode, report_success() must NOT pin the cursor to the
        winning account — otherwise rotation degenerates back to sticky."""
        print("\n=== Test: round_robin report_success keeps rotating ===")
        monkeypatch.setattr("kiro.account_manager.ACCOUNT_STRATEGY", "round_robin")

        manager = await self._build_three_account_manager(tmp_path, mock_list_models_response)
        ids = list(manager._accounts.keys())
        manager._current_account_index = 0

        a = await manager.get_next_account("claude-opus-4.5")
        await manager.report_success(a.id, "claude-opus-4.5")
        b = await manager.get_next_account("claude-opus-4.5")
        await manager.report_success(b.id, "claude-opus-4.5")
        c = await manager.get_next_account("claude-opus-4.5")

        print(f"Picked: {[a.id, b.id, c.id]}")
        assert {a.id, b.id, c.id} == set(ids)  # all three visited

    @pytest.mark.asyncio
    async def test_sticky_report_success_still_pins_cursor(
        self, tmp_path, mock_list_models_response, monkeypatch,
    ):
        """In sticky mode, report_success() still pins cursor (regression guard)."""
        print("\n=== Test: sticky report_success pins cursor ===")
        monkeypatch.setattr("kiro.account_manager.ACCOUNT_STRATEGY", "sticky")

        manager = await self._build_three_account_manager(tmp_path, mock_list_models_response)
        ids = list(manager._accounts.keys())
        manager._current_account_index = 0

        # Manually pick the 3rd account and report success
        target_id = ids[2]
        await manager.report_success(target_id, "claude-opus-4.5")

        assert manager._current_account_index == 2
```

- [ ] **Step 2: Run tests to verify failure**

Run: `pytest tests/unit/test_account_manager.py::TestAccountManagerRoundRobinStrategy::test_round_robin_report_success_does_not_reset_cursor -v`
Expected: FAIL. After `report_success(a.id)`, cursor is pinned back to a's index, so the next `get_next_account` advances cursor to a+1 (deterministic) — but if a and b end up pointing to the same account because the cursor pin overrides the advance, the set won't equal {ids[0], ids[1], ids[2]}.

- [ ] **Step 3: Modify `report_success()` to skip pinning in round_robin mode**

In `kiro/account_manager.py`, locate lines 794-802 and replace the `# GLOBAL STICKY: Update global current_account_index` block with:

```python
            # Update global cursor on success — ONLY in sticky mode.
            # In round_robin mode, the cursor is advanced inside get_next_account()
            # on every fresh call; pinning it here would defeat rotation.
            if ACCOUNT_STRATEGY == "sticky":
                all_account_ids = list(self._accounts.keys())
                try:
                    successful_index = all_account_ids.index(account_id)
                    if self._current_account_index != successful_index:
                        self._current_account_index = successful_index
                        self._dirty = True
                except ValueError:
                    pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_account_manager.py::TestAccountManagerRoundRobinStrategy -v`
Expected: 5 passed.

- [ ] **Step 5: Run the entire test suite for the module**

Run: `pytest tests/unit/test_account_manager.py -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
rtk git add kiro/account_manager.py tests/unit/test_account_manager.py
rtk git commit -m "feat(account-system): skip sticky-pin on success in round_robin mode"
```

---

## Task 4: Surface the toggle in `.env.example`

**Files:**
- Modify: `.env.example` (insert after line 147 `ACCOUNTS_STATE_FILE` block, before the Circuit Breaker section at line 149)

- [ ] **Step 1: Add the documentation block**

Insert the following block into `.env.example` immediately after the `ACCOUNTS_STATE_FILE` comment block (after line 147):

```env

# Account selection strategy (only effective when 2+ accounts configured)
# Options:
#   sticky       (default) Keep using one account until it fails; best for
#                cache hit rate and minimal token refresh churn.
#   round_robin  Rotate to the next account on every request to spread load.
#                Circuit Breaker / failover still applies.
# Unknown values fall back to "sticky".
# ACCOUNT_STRATEGY=sticky
```

- [ ] **Step 2: Verify the file is well-formed**

Run: `grep -nE "^# ?ACCOUNT_STRATEGY" .env.example`
Expected: one line matching `# ACCOUNT_STRATEGY=sticky`.

- [ ] **Step 3: Commit**

```bash
rtk git add .env.example
rtk git commit -m "docs(env): document ACCOUNT_STRATEGY toggle"
```

---

## Task 5: Integration smoke test

**Files:**
- Test only (no new files)

- [ ] **Step 1: Run the full unit test suite**

Run: `pytest tests/unit -x -q`
Expected: all tests pass.

- [ ] **Step 2: Manual smoke (round_robin)**

In a separate shell, with at least 2 accounts configured:

```bash
ACCOUNT_STRATEGY=round_robin LOG_LEVEL=INFO python main.py &
SERVER_PID=$!
sleep 3
# Fire 4 small requests
for i in 1 2 3 4; do
  curl -s -X POST http://localhost:8000/v1/chat/completions \
    -H "Authorization: Bearer $PROXY_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"claude-sonnet-4-5","messages":[{"role":"user","content":"hi"}],"max_tokens":4}' \
    > /dev/null
done
# Inspect state.json — current_account_index should have advanced
rtk read state.json | grep current_account_index
kill $SERVER_PID
```

Expected: `current_account_index` is not stuck at 0; logs show different account IDs being picked across the 4 requests.

- [ ] **Step 3: Manual smoke (sticky regression guard)**

```bash
ACCOUNT_STRATEGY=sticky LOG_LEVEL=INFO python main.py &
SERVER_PID=$!
sleep 3
for i in 1 2 3 4; do
  curl -s -X POST http://localhost:8000/v1/chat/completions \
    -H "Authorization: Bearer $PROXY_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{"model":"claude-sonnet-4-5","messages":[{"role":"user","content":"hi"}],"max_tokens":4}' \
    > /dev/null
done
kill $SERVER_PID
```

Expected: logs show the same account ID across all 4 requests (sticky behaviour preserved).

- [ ] **Step 4: Commit the final state if anything trailing needs cleanup**

```bash
rtk git status
# If clean, no commit needed. Otherwise:
# rtk git add -A && rtk git commit -m "chore: post-smoke cleanup"
```

---

## Self-Review Notes

- **Spec coverage:** The spec was "add a round_robin polling strategy alongside the existing sticky failover, without breaking it." Task 1 adds config; Task 2 changes selection; Task 3 stops `report_success` from defeating rotation; Task 4 documents; Task 5 verifies end-to-end. Single-account fast path (`account_manager.py:673-704`) is intentionally untouched — round-robin has no meaning with one account.
- **No placeholders:** every step has concrete code or commands.
- **Type consistency:** `ACCOUNT_STRATEGY` is referenced consistently as `str` ∈ {`"sticky"`, `"round_robin"`}; `_current_account_index: int` is unchanged.
- **State persistence:** `state.json` schema is unchanged. `current_account_index` is now reused as the round-robin cursor — on restart it resumes from where it left off, which is the correct behaviour.
- **Concurrency:** all mutations to `_current_account_index` happen inside `async with self._lock`, preserved.
- **Failover correctness:** when `exclude_accounts` is non-empty (mid-retry), round_robin does NOT advance the cursor — this keeps the failover walk deterministic and visits the right set of remaining accounts.
