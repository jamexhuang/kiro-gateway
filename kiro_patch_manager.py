import os
import shutil
import hashlib
import re

KIRO_DIR = os.path.expanduser("~/Documents/github/kiro-gateway/kiro")
BACKUP_DIR = os.path.expanduser("~/Documents/github/kiro-gateway/kiro_backups")

def ensure_backups():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    for filename in ["converters_core.py", "parsers.py"]:
        src = os.path.join(KIRO_DIR, filename)
        dst = os.path.join(BACKUP_DIR, filename)
        if not os.path.exists(dst) and os.path.exists(src):
            shutil.copy2(src, dst)
            print(f"Created initial backup for {filename}")

def rollback():
    if not os.path.exists(BACKUP_DIR):
        print("No backups found. Cannot rollback.")
        return
    for filename in ["converters_core.py", "parsers.py"]:
        src = os.path.join(BACKUP_DIR, filename)
        dst = os.path.join(KIRO_DIR, filename)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            print(f"Restored {filename} from backup")
    print("Rollback complete.")

def apply_patch():
    ensure_backups()
    print("Applying patch...")
    
    # --- Patch converters_core.py ---
    cc_path = os.path.join(KIRO_DIR, "converters_core.py")
    with open(cc_path, "r") as f:
        cc_content = f.read()
    
    # 1. Inject global cache and hashing function
    if "TOOL_NAME_CACHE =" not in cc_content:
        injection = """
import hashlib
TOOL_NAME_CACHE = {}

def get_short_tool_name(long_name: str) -> str:
    if len(long_name) <= 64:
        return long_name
    hash_suffix = hashlib.md5(long_name.encode()).hexdigest()[:10]
    short_name = long_name[:53] + "_" + hash_suffix
    TOOL_NAME_CACHE[short_name] = long_name
    return short_name

def restore_tool_name(short_name: str) -> str:
    return TOOL_NAME_CACHE.get(short_name, short_name)
"""
        cc_content = cc_content.replace("from loguru import logger", "from loguru import logger\n" + injection)

    # 2. Disable validation error safely using regex to catch the whole block
    pattern = r'(raise ValueError\(\s*f"Tool name\(s\) exceed Kiro API limit.*?\"\s*\))'
    if re.search(pattern, cc_content, re.DOTALL):
        cc_content = re.sub(
            pattern, 
            r'logger.warning(f"Auto-shortening tool names exceeding 64 characters limit")', 
            cc_content, 
            flags=re.DOTALL
        )

    # 3. Patch convert_tools_to_kiro_format
    if '"name": get_short_tool_name(tool.name),' not in cc_content:
        cc_content = cc_content.replace(
            '"name": tool.name,',
            '"name": get_short_tool_name(tool.name),'
        )

    # 4. Patch extract_tool_uses_from_message
    if 'tu["toolUse"]["name"] = get_short_tool_name(tc["function"]["name"])' not in cc_content:
        cc_content = cc_content.replace(
            'tu["toolUse"]["name"] = tc["function"]["name"]',
            'tu["toolUse"]["name"] = get_short_tool_name(tc["function"]["name"])'
        )
        cc_content = cc_content.replace(
            'tu["toolUse"]["name"] = item["name"]',
            'tu["toolUse"]["name"] = get_short_tool_name(item["name"])'
        )

    with open(cc_path, "w") as f:
        f.write(cc_content)
    print("Patched converters_core.py")

    # --- Patch parsers.py ---
    p_path = os.path.join(KIRO_DIR, "parsers.py")
    with open(p_path, "r") as f:
        p_content = f.read()

    if "from kiro.converters_core import restore_tool_name" not in p_content:
        p_content = p_content.replace(
            "from kiro.converters_core import UnifiedMessage, UnifiedTool",
            "from kiro.converters_core import UnifiedMessage, UnifiedTool, restore_tool_name"
        )
    
    if "restored_name = restore_tool_name(tool_name)" not in p_content:
        p_content = p_content.replace(
            'tool_name = data.get("name", "")',
            'tool_name = data.get("name", "")\n                restored_name = restore_tool_name(tool_name)\n                tool_name = restored_name'
        )

    with open(p_path, "w") as f:
        f.write(p_content)
    print("Patched parsers.py")
    print("Patch apply complete!")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "rollback":
        rollback()
    else:
        apply_patch()
