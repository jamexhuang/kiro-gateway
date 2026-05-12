# 模型重定向記錄：Haiku -> Sonnet

## 變更摘要
為了優化模型回應品質，我們實施了模型家族級別的重定向機制，將所有針對 **Haiku** 家族的請求自動導向至 **Sonnet** 家族。

## 修改內容

### 1. 配置更新 (`kiro/config.py`)
新增了 `MODEL_FAMILY_ALIASES` 配置項，用於定義家族間的映射關係。
```python
MODEL_FAMILY_ALIASES: Dict[str, str] = {
    "haiku": "sonnet",
}
```

### 2. 解析邏輯升級 (`kiro/model_resolver.py`)
- **`ModelResolver` 類別**：在 `resolve` 流程中新增了「家族重定向」層級（Layer 1.5）。在完成名稱標準化後，系統會檢查模型所屬家族，若存在於重定向名單中，則自動替換名稱。
- **`get_model_id_for_kiro` 函數**：新增 `family_aliases` 參數，使獨立呼叫此函數的地方也能支援家族映射。

### 3. 系統整合更新
- **`kiro/account_manager.py`**：在初始化 `ModelResolver` 時傳入家族別名配置。
- **`kiro/routes_anthropic.py`**：更新路由邏輯中的模型 ID 獲取方式。
- **`kiro/converters_anthropic.py` & `kiro/converters_openai.py`**：更新轉換器邏輯，確保在將請求送往 Kiro API 前已完成家族重定向。

### 4. 測試驗證 (`tests/unit/test_model_resolver.py`)
新增了 `TestFamilyRedirection` 測試類別，驗證以下情境：
- `claude-haiku-4.5` 正確重定向至 `claude-sonnet-4.5`。
- 帶連字號的格式 `claude-haiku-4-5` 也能正確重定向。
- 當重定向後的模型不在快取中時，能正確執行 Passthrough（透傳）邏輯。

## 預期行為
- 使用者請求 `claude-haiku-*` 時，後端將實際呼叫 `claude-sonnet-*`。
- 日誌中會記錄：`Family redirection: 'claude-haiku-4.5' -> 'claude-sonnet-4.5' (triggered by family rule: haiku->sonnet)`。

---
**日期**: 2026-05-12
**執行者**: Gemini CLI
