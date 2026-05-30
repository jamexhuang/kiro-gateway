# -*- coding: utf-8 -*-

"""
Unit tests for AccountManager CRUD operations (reload_credentials, add_account_entry,
remove_account_entry, update_account_entry).
"""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

from kiro.account_manager import AccountManager, Account
from kiro.auth import AuthType


@pytest.fixture
def tmp_creds_and_state(tmp_path):
    """Fixture that creates dummy credentials and state files."""
    creds_file = tmp_path / "credentials.json"
    state_file = tmp_path / "state.json"
    
    # Pre-populate credentials.json with a valid initial config
    initial_creds = [
        {
            "type": "refresh_token",
            "refresh_token": "token_1_abc123",
            "comment": "Account 1",
            "enabled": True
        }
    ]
    with open(creds_file, 'w', encoding='utf-8') as f:
        json.dump(initial_creds, f, indent=2)
        
    return creds_file, state_file


@pytest.mark.asyncio
async def test_reload_credentials_and_rebuild_mappings(tmp_creds_and_state):
    """Test that reload_credentials loads accounts, preserves active ones, and drops stale ones."""
    creds_file, state_file = tmp_creds_and_state
    
    manager = AccountManager(str(creds_file), str(state_file))
    await manager.load_credentials()
    
    # 1 account should be loaded
    assert len(manager._accounts) == 1
    initial_account_id = list(manager._accounts.keys())[0]
    
    # Let's write a new config with 2 accounts (one new, one existing)
    new_creds = [
        {
            "type": "refresh_token",
            "refresh_token": "token_1_abc123", # same as before
            "comment": "Account 1",
            "enabled": True
        },
        {
            "type": "refresh_token",
            "refresh_token": "token_2_xyz789", # new
            "comment": "Account 2",
            "enabled": True
        }
    ]
    with open(creds_file, 'w', encoding='utf-8') as f:
        json.dump(new_creds, f, indent=2)
        
    # Reload
    await manager.reload_credentials()
    
    # Now should have 2 accounts
    assert len(manager._accounts) == 2
    assert initial_account_id in manager._accounts
    
    # Disable Account 1 in config
    new_creds[0]["enabled"] = False
    with open(creds_file, 'w', encoding='utf-8') as f:
        json.dump(new_creds, f, indent=2)
        
    # Reload
    await manager.reload_credentials()
    
    # Now should only have 1 active account (Account 2)
    assert len(manager._accounts) == 1
    assert initial_account_id not in manager._accounts


@pytest.mark.asyncio
@patch("kiro.account_manager.AccountManager._initialize_account", new_callable=AsyncMock)
async def test_add_account_entry_refresh_token(mock_init, tmp_creds_and_state):
    """Test adding a refresh_token type account entry."""
    creds_file, state_file = tmp_creds_and_state
    mock_init.return_value = True
    
    manager = AccountManager(str(creds_file), str(state_file))
    await manager.load_credentials()
    
    new_account_entry = {
        "type": "refresh_token",
        "refresh_token": "token_added_at_runtime",
        "comment": "Runtime Added Account"
    }
    
    # Add entry
    account_id = await manager.add_account_entry(new_account_entry)
    
    # Verify file was written
    with open(creds_file, 'r', encoding='utf-8') as f:
        config = json.load(f)
        
    # Should have 2 entries now
    assert len(config) == 2
    assert config[1]["refresh_token"] == "token_added_at_runtime"
    assert config[1]["comment"] == "Runtime Added Account"
    assert config[1]["enabled"] is True
    
    # Verify account was initialized
    assert account_id in manager._accounts
    mock_init.assert_called_with(account_id)


@pytest.mark.asyncio
@patch("kiro.account_manager.AccountManager._initialize_account", new_callable=AsyncMock)
async def test_add_account_entry_json_payload(mock_init, tmp_creds_and_state, tmp_path):
    """Test adding a JSON token payload account entry."""
    creds_file, state_file = tmp_creds_and_state
    mock_init.return_value = True
    
    # Set ACCOUNTS_CONFIG_FILE or pass to manager
    manager = AccountManager(str(creds_file), str(state_file))
    await manager.load_credentials()
    
    # Valid raw payload
    valid_payload = {
        "refreshToken": "another_refresh_token",
        "comment": "Copied payload account"
    }
    
    account_id = await manager.add_account_entry(valid_payload)
        
    assert "accounts/account_" in account_id
    assert account_id in manager._accounts
    mock_init.assert_called_with(account_id)
    
    # Check that file was created and is valid
    # In the test, the file was written to `accounts/account_*.json` in local directory.
    # Let's clean up if it was written. (We'll find the written path in config).
    with open(creds_file, 'r', encoding='utf-8') as f:
        config = json.load(f)
        
    assert len(config) == 2
    assert config[1]["type"] == "json"
    created_path = Path(config[1]["path"])
    
    if created_path.exists():
        created_path.unlink() # Cleanup


@pytest.mark.asyncio
async def test_remove_account_entry(tmp_creds_and_state):
    """Test removing an account entry by ID."""
    creds_file, state_file = tmp_creds_and_state
    
    manager = AccountManager(str(creds_file), str(state_file))
    await manager.load_credentials()
    
    account_id = list(manager._accounts.keys())[0]
    
    # Remove it
    success = await manager.remove_account_entry(account_id)
    assert success is True
    
    # Verify credentials.json is empty
    with open(creds_file, 'r', encoding='utf-8') as f:
        config = json.load(f)
    assert len(config) == 0
    assert len(manager._accounts) == 0


@pytest.mark.asyncio
async def test_update_account_entry(tmp_creds_and_state):
    """Test updating account enabled state or comment."""
    creds_file, state_file = tmp_creds_and_state
    
    manager = AccountManager(str(creds_file), str(state_file))
    await manager.load_credentials()
    
    account_id = list(manager._accounts.keys())[0]
    
    # 1. Toggle disabled to True
    success = await manager.update_account_entry(account_id, disabled=True)
    assert success is True
    
    # Verify in config
    with open(creds_file, 'r', encoding='utf-8') as f:
        config = json.load(f)
    assert config[0]["enabled"] is False
    assert len(manager._accounts) == 0 # because it reloaded and it's disabled!
    
    # 2. Toggle disabled to False and update comment
    success = await manager.update_account_entry(account_id, disabled=False, comment="Updated comment!")
    assert success is True
    
    with open(creds_file, 'r', encoding='utf-8') as f:
        config = json.load(f)
    assert config[0]["enabled"] is True
    assert config[0]["comment"] == "Updated comment!"
    
    # Reloaded and active again
    assert len(manager._accounts) == 1
