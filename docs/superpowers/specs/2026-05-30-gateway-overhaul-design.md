# Kiro Gateway Overhaul — Design Spec

Date: 2026-05-30
Status: approved-by-default (user said "全部都做好"); locked decisions, course-correct anytime.

Covers the user's 4 asks. Tasks 1 + 3-hotfix are **already done & verified on prod** (LXC 123).
Remaining work below is sequenced into 3 sprints.

---

## Sprint 1 — Dashboard Passkey Auth + Account CRUD  (closes task 3 fully)

### Goal
Replace the shared-API-key dashboard access with real **single-admin WebAuthn/FIDO2 (passkey)**
login, and let the admin **view / add / edit / delete Kiro accounts** from the UI (so credential
provisioning never needs SSH again). Remove the auto-key-injection entirely.

### Decisions (locked)
- **Library:** `webauthn` (py_webauthn 2.7.1) — audited, standard. Added to requirements.txt.
- **Scope:** single admin identity; multiple passkeys (laptop + phone) allowed. No multi-user, no roles. (YAGNI)
- **Storage:** new `kiro/dashboard_auth.py` → `DashboardAuthStore`, persisted to JSON
  `DASHBOARD_AUTH_FILE` (default `dashboard_auth.json` in cwd, same persistence as credentials.json).
  Holds: `credentials[]` ({id, public_key, sign_count, transports, nickname, created_at, last_used}),
  `session_secret` (random 32B, generated once). Challenges kept in-memory w/ short TTL.
- **Sessions:** stateless signed cookie `kiro_dash_session` = base64url(payload).HMAC-SHA256(session_secret).
  payload = {sub:"admin", exp}. Default TTL 12h. HttpOnly, Secure, SameSite=Lax. No new dep (hmac/hashlib).
- **RP config:** `DASHBOARD_RP_ID` (default `edge.jamex.me`), `DASHBOARD_RP_NAME` ("Kiro Gateway"),
  `DASHBOARD_ORIGIN` (default `https://edge.jamex.me`). localhost dev → rp_id `localhost`.
- **Bootstrap:** first passkey registration is gated by the existing `PROXY_API_KEY` (only when 0 passkeys
  exist). Once ≥1 passkey exists, registering more requires an authenticated session. Prevents a public
  attacker registering first.
- **Authorization unification:** `require_dashboard_auth` accepts EITHER a valid session cookie OR the
  `PROXY_API_KEY` (keeps CLI/programmatic access). Loopback dev (no proxy headers) auto-issues a session.
  The old `window.KIRO_AUTO_KEY` injection is removed.

### Endpoints (new, under `/dashboard/api/auth`)
`GET status` · `POST register/begin` · `POST register/complete` · `POST login/begin` ·
`POST login/complete` · `POST logout` · `GET passkeys` · `DELETE passkeys/{id}`

### Account CRUD (new, under `/dashboard/api/accounts`, all require auth)
- `GET` → list (id, email, comment, healthy, circuit_state, model_count, disabled)
- `POST` → body = Kiro/cockpit token JSON or {refresh_token}. Validate via
  `_is_supported_json_credentials_payload`; write `accounts/account_<n>.json`; append to credentials.json;
  hot-reload account_manager. (Natively supports cockpit `kiro_auth_token_raw` — auth.py:82.)
- `DELETE {id}` → remove file + credentials.json entry + reload
- `PATCH {id}` → {disabled?, comment?} (enable/disable + rename)
- Requires new `AccountManager` runtime methods: `reload_credentials()`, `add_account_entry()`,
  `remove_account_entry()`, `set_account_disabled()`. Reload must not drop in-flight request state.

### Frontend
Login screen (passkey button via native `navigator.credentials`, base64url helpers inline — no CDN).
"账户管理" panel: list + add (paste/upload JSON) + delete + enable/disable. "通行密钥" panel: list/add/remove passkeys.

### Tests
Unit: HMAC session sign/verify, store load/save, bootstrap gating, require_dashboard_auth matrix (session ok,
key ok, neither → 401), challenge TTL. WebAuthn verify steps mocked. Account CRUD: add/validate/reject-bad/
delete/reload. Route-level: protected endpoints reject unauthenticated.

---

## Sprint 2 — Routing UX redesign + dynamic model sync  (task 2)

### Decisions (locked)
- **Dynamic models:** routing UI populates model dropdowns from the live `/dashboard/api/models` (Kiro
  `/ListAvailableModels`) instead of hardcoded lists. Add `claude-opus-4.8` and make the
  `opus-4.7→4.6` redirect data-driven (editable in UI, not hardcoded in config.py).
- **Routing model:** keep 3 clear modes — `passthrough` / `manual override` / `redirect map` — but redesign
  the panel: per-rule rows, live dropdowns, and an explicit **Apply** with a **preview** ("requests for X →
  Y") + confirm before commit (reuse `/dashboard/api/routing/test`).
- Config sprawl: move static aliases/redirects/fallbacks toward runtime `RoutingConfig` where they belong;
  config.py keeps only seed defaults.

### Tests
Routing decision matrix incl opus-4.8; preview endpoint; dynamic-list fallback when Kiro fetch fails.

---

## Sprint 3 — Architecture cleanup  (task 4) — keep tests green throughout

- Remove `.venv/` from git tracking (add to .gitignore); remove `kiro_backups/`.
- Audit loose root scripts (`manual_api_test.py`, `payload_monitor.py`, `benchmark_models.py`,
  `list_remote_models.py`, `kiro_patch_manager.py`) — confirm not imported, then move to `scripts/` or delete.
- Consolidate config; tidy module boundaries only where it serves the above. No gratuitous refactor.
- Full suite green after each step.

---

## Deploy
Each sprint: implement + test locally → commit → scp changed files to LXC 123 `/opt/kiro-gateway`
(+ `pip install` new deps) → `systemctl restart kiro-gateway` → verify via public + loopback. Back up first.

## Out of scope (YAGNI)
Multi-user/roles, password fallback, account auto-discovery, LXC 116, Docker re-architecture.
