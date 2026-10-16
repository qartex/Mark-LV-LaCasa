# Cannanas JARVIS Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the Cannanas MCP adapter into the Mark-LV headless JARVIS stack on `lacasa-server`, exposing read and confirmed write actions through the existing plugin system while preserving half-duplex, phone auth, Spotify, and HA functionality.

**Architecture:** The adapter runs as a persistent STDIO subprocess of JARVIS. A new `plugins/cannanas.go` wrapper translates between the Mark-LV plugin interface and MCP tool calls. The headless registry is extended with a closed `cannanas` action enum. Confirmation state is owned by the adapter; JARVIS only forwards pre-approved confirms from the host coordinator.

**Tech Stack:** Go (adapter), Python (Mark-LV host), `mcp-go` client library.

**Spec:** `docs/superpowers/specs/2026-10-16-cannanas-jarvis-integration-design.md`
**Prerequisite:** `docs/superpowers/plans/2026-10-16-cannanas-mcp-adapter.md` (Task 1–8 complete, adapter binary built).

## Global Constraints

- The adapter runs on `lacasa-server` only; `lacasa15` remains the Hermes/Cannanas development host.
- Existing services (JARVIS, Spotify ducking, HA read-only, phone auth) must remain green after integration.
- The `cannabis_status` plugin is denied at the registry level; it cannot be re-enabled.
- Durable device provenance (`stable_device_id`, `auth_session_id`, `user_turn_id`) flows from ingress through tool dispatch to confirmation.
- Confirmation tokens are host-side only; Gemini never sees them.
- Audit log is secret-free, append-only, mode 0640, written before POST.
- Adapter restart invalidates pending confirmations; fresh prepare required.
- No new HTTP listeners; adapter communicates only via STDIO.
- Work in `/root/Mark-LV-LaCasa/.worktrees/cannanas-integration` on branch `design/cannanas-jarvis`.
- Do not modify canonical MCP server at `/home/lacasa15/services/cannanas-mcp/`.
- Preserve all existing tests; add new tests only.

## Review Focus

1. **Provenance chain:** HTTP text, WebSocket text, and phone PCM all carry immutable envelope with stable device ID through to tool dispatch.
2. **Enum closure:** only approved actions reach the adapter; arbitrary strings rejected before dispatch.
3. **Confirmation fence:** concurrent/replayed confirms yield exactly one POST; restart clears pending state.
4. **Audit completeness:** every write has a pre-POST audit entry; missing audit blocks POST.
5. **Service isolation:** adapter crash or timeout does not affect Spotify ducking, HA reads, or phone audio.

---

### Task 1: Plugin wrapper skeleton and registry integration

**Files:**
- Create: `plugins/cannabis_status.go` (mark as deprecated/denied)
- Create: `plugins/cannanas.go`
- Modify: `core/headless.go` — extend `read_only_registry()`
- Create: `tests/test_cannabis_status_denied.go`
- Create: `tests/test_cannanas_plugin.go`

**Interfaces:**
- `PLUGIN` spec in `plugins/cannanas.go`: name `"cannanas"`, description, parameters `{action: ENUM, ...}`.
- `Run(action string, params map[string]any, player=None, session_memory=None) (string, error)` — validates action against enum, dispatches to adapter.
- `AllowedActions() []string` — returns closed list: `overview`, `find_zones`, `find_strains`, `find_plants`, `find_propagation_material`, `list_recent_batches`, `list_plants`, `count_plants_by_room`, `prepare_plant_create`, `confirm_plant_create`, `prepare_strain_create`, `confirm_strain_create`, `prepare_batch_create`, `confirm_batch_create`, `prepare_plant_update`, `confirm_plant_update`, `prepare_batch_update`, `confirm_batch_update`, `prepare_diary_create`, `confirm_diary_create`, `get_adapter_info`.
- `cannabis_status` plugin registered but immediately rejected with "deprecated" message.

- [ ] **Step 1: Write failing registry tests**
  - `TestCannabisStatusDenied`, `TestCannanasActionEnumComplete`, `TestCannanasRunValidAction`, `TestCannanasRunInvalidActionRejected`.

- [ ] **Step 2: Implement plugin wrapper skeleton**
  - Empty `Run()` returning error for now; populate enum list.
  - Register in `read_only_registry()` under conditional compile tag or feature flag.

- [ ] **Step 3: Run tests GREEN**
  - Command: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_cannabis_status_denied tests.test_cannanas_plugin -v`
  - Expected: all pass.

- [ ] **Step 4: Commit**
  - `git add plugins/cannabis_status.go plugins/cannanas.go core/headless.go tests/test_cannabis_status_denied.go tests/test_cannanas_plugin.go`
  - `git commit -m "feat(cannabis): add cannanas plugin wrapper and deny cannabis_status"`

---

### Task 2: STDIO adapter client with lifecycle management

**Files:**
- Create: `core/cannanas_client.go`
- Create: `tests/test_cannanas_client.go`

**Interfaces:**
- `AdapterClient` struct manages STDIO subprocess lifecycle.
- `NewAdapterClient(adapterPath string, cfg Config) (*AdapterClient, error)` — validates binary exists, starts process, performs MCP initialize handshake, verifies tool list matches expected set.
- `Call(tool string, params json.RawMessage) (json.RawMessage, error)` — sends JSON-RPC request, reads response, enforces 25s timeout.
- `Close()` — graceful shutdown, wait for process exit.
- Restart policy: one restart on unexpected exit; after restart, invalidate pending confirmations.

- [ ] **Step 1: Write failing client tests**
  - `TestNewClientStartsProcess`, `TestNewClientVerifiesToolList`, `TestNewClientRejectsMissingBinary`, `TestCallTimesOutAfter25s`, `TestRestartAfterCrash`, `TestCloseStopsProcess`.

- [ ] **Step 2: Implement STDIO client**
  - Use `os/exec` for process management, `jsonrpc2` or manual JSON parsing for protocol.
  - Initialize handshake: send `initialize` params, expect `initialize` result with `capabilities` and `serverInfo`.
  - Tool list verification: assert expected tool names present.

- [ ] **Step 3: Run client tests GREEN**
  - Command: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_cannanas_client -v`
  - Expected: all pass.

- [ ] **Step 4: Commit**
  - `git add core/cannanas_client.go tests/test_cannanas_client.go`
  - `git commit -m "feat(cannabis): add STDIO adapter client with lifecycle"`

---

### Task 3: Ingress envelope and provenance propagation

**Files:**
- Modify: `dashboard/server.py` — HTTP and WebSocket ingress
- Modify: `main.py` — turn dispatch
- Create: `tests/test_ingress_provenance.go` (or Python equivalent)

**Interfaces:**
- Ingress envelope: `{stable_device_id, auth_session_id, capture_id, user_turn_id, channel, text_or_pcm}`.
- `stable_device_id` from durable credential lookup; `auth_session_id` from bearer; `user_turn_id` monotonically increasing per session.
- Turn dispatch passes envelope to plugin `Run()` via `session_memory` or explicit parameter.
- PCM transcription finalization creates `user_turn_id`; tool calls before finalization are rejected.

- [ ] **Step 1: Write failing provenance tests**
  - `TestHTTPTextEnvelopesStableDeviceID`, `TestWebSocketTextEnvelopesAuthSession`, `TestPCMFinalizationCreatesTurnID`, `TestToolCallBeforeFinalizationRejected`.

- [ ] **Step 2: Implement envelope injection**
  - Modify dashboard to attach provenance to each turn context.
  - Modify `main.py` `_execute_tool` to pass envelope to plugins.

- [ ] **Step 3: Run tests GREEN**
  - Command: run relevant test suite.
  - Expected: all pass.

- [ ] **Step 4: Commit**
  - `git add dashboard/server.py main.py tests/test_ingress_provenance.py`
  - `git commit -m "feat(cannabis): propagate provenance envelope through ingress"`

---

### Task 4: Confirmation coordinator with host-side fencing

**Files:**
- Create: `core/cannanas_confirmation.go`
- Create: `tests/test_confirmation_coordinator.go`

**Interfaces:**
- `ConfirmationCoordinator` — in-memory, thread-safe map of pending confirms.
- `StorePending(token string, pending PendingConfirmation) error` — idempotent by token.
- `Consume(token string) (PendingConfirmation, bool)` — atomic compare-and-swap pending→executing.
- `InvalidateAll()` — called on adapter restart.
- `PendingConfirmation` struct: `{Action, PayloadHash, Preview, AdapterBootID, SessionID, DeviceID, CreatedAt, ExpiresAt}`.
- TTL: 5 minutes.

- [ ] **Step 1: Write failing coordinator tests**
  - `TestStoreAndConsume`, `TestConsumeRejectsReplay`, `TestConsumeRejectsExpired`, `TestInvalidateAllClearsPending`, `TestConcurrentConfirmsYieldOneExecutor`.

- [ ] **Step 2: Implement coordinator**
  - Use `sync.Map` with compare-and-swap semantics.
  - TTL check on consume; background cleanup goroutine.

- [ ] **Step 3: Run tests GREEN**
  - Command: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_confirmation_coordinator -v`
  - Expected: all pass.

- [ ] **Step 4: Commit**
  - `git add core/cannanas_confirmation.go tests/test_confirmation_coordinator.go`
  - `git commit -m "feat(cannabis): add confirmation coordinator with fencing"`

---

### Task 5: Wire plugin to adapter and confirmation coordinator

**Files:**
- Modify: `plugins/cannanas.go`
- Modify: `core/headless.go`
- Create: `tests/test_cannanas_integration.go`

**Interfaces:**
- `Run()` now delegates to `AdapterClient.Call()` for read actions.
- `prepare_*` actions call adapter, store pending confirmation, return preview.
- `confirm_*` actions check coordinator consume, forward token to adapter, return result.
- Host coordinator (in `main.py` or new `core/cannanas_host.py`) manages the lifecycle: starts adapter, injects boot ID, handles restart.

- [ ] **Step 1: Write failing integration tests**
  - `TestReadActionCallsAdapter`, `TestPrepareStoresPending`, `TestConfirmConsumesPendingAndCallsAdapter`, `TestConfirmRejectsWrongToken`, `TestRestartInvalidatesPending`.

- [ ] **Step 2: Implement wiring**
  - Connect plugin → client → coordinator.
  - Add boot ID to adapter initialize params.

- [ ] **Step 3: Run tests GREEN**
  - Command: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_cannanas_integration -v`
  - Expected: all pass.

- [ ] **Step 4: Commit**
  - `git add plugins/cannanas.go core/headless.go core/cannanas_host.py tests/test_cannanas_integration.go`
  - `git commit -m "feat(cannabis): wire plugin to adapter and confirmation coordinator"`

---

### Task 6: Audit log and secret sanitization

**Files:**
- Create: `core/cannanas_audit.go`
- Modify: `core/cannanas_client.go` — add sanitizer
- Create: `tests/test_audit_log.go`

**Interfaces:**
- `AuditLog` — append-only JSONL file, mode 0640, path configurable.
- `LogEntry`: `{timestamp, action, payload_hash, preview, device_id, session_id, state, result_id}`.
- Sanitizer strips `Bearer \S+` and confirmation tokens from all logged strings.
- Audit failure (write error) returns error to caller; write actions fail closed.

- [ ] **Step 1: Write failing audit tests**
  - `TestAuditLogWritesEntry`, `TestAuditLogStripsBearer`, `TestAuditLogStripsToken`, `TestAuditFailureFailsWrite`.

- [ ] **Step 2: Implement audit log and sanitizer**
  - File append with fsync.
  - Regex sanitizer for bearer and token patterns.

- [ ] **Step 3: Run tests GREEN**
  - Command: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_audit_log -v`
  - Expected: all pass.

- [ ] **Step 4: Commit**
  - `git add core/cannanas_audit.go core/cannanas_client.go tests/test_audit_log.go`
  - `git commit -m "feat(cannabis): add audit log and secret sanitization"`

---

### Task 7: Systemd service and deployment artifacts

**Files:**
- Create: `deploy/cannanas-adapter.service`
- Create: `deploy/cannanas-adapter.env` (template, gitignored)
- Create: `scripts/deploy-adapter.sh`
- Modify: `README.md` or `docs/deployment.md`

**Interfaces:**
- Systemd unit: `Type=notify`, user `lacasa`, credential file `config/cannanas_credentials.json` (mode 0600), logs to journal.
- Environment template: `ADAPTER_PATH`, `AUDIT_LOG_PATH`, `LOG_LEVEL`.
- Deploy script: copies binary, sets permissions, enables service.

- [ ] **Step 1: Write failing deploy tests**
  - `TestServiceFileExists`, `TestServiceValidSyntax`, `TestDeployScriptCopiesBinary`.

- [ ] **Step 2: Create deployment artifacts**
  - Write systemd unit, env template, deploy script.

- [ ] **Step 3: Run tests GREEN**
  - Command: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest tests.test_deployment -v`
  - Expected: all pass.

- [ ] **Step 4: Commit**
  - `git add deploy/ scripts/ README.md`
  - `git commit -m "feat(cannabis): add systemd service and deployment artifacts"`

---

### Task 8: End-to-end acceptance and documentation

**Files:**
- Create: `tests/test_acceptance_e2e.go`
- Modify: `docs/server-voice-quickstart.md`
- Create: `docs/cannanas-jarvis-integration.md`

**Acceptance criteria:**
1. All 23 criteria from design spec verified (mocked where live API unavailable).
2. Existing Spotify, HA, phone auth, half-duplex tests remain green.
3. Secret scan: no bearer tokens, confirmation tokens, or raw secrets in code, tests, or logs.
4. Deployment SHA matches artifact SHA.
5. Live staging test (with user approval): create plant, strain, batch; verify IDs; attempt replay; verify denial.

- [ ] **Step 1: Write acceptance test skeleton**
  - One test per criterion; skip live API tests with `t.Skip("no live API")`.

- [ ] **Step 2: Run full test suite**
  - Command: `/root/.hermes/cache/scratch/marklv-task1-venv/bin/python -m unittest discover -s tests -v`
  - Expected: all pass.

- [ ] **Step 3: Run secret scan**
  - Command: `grep -r "cannanas\|bearer\|club_id\|confirmation_token" --include="*.go" --include="*.py" --exclude-dir=".git" | grep -v "_test.go" | grep -v "REDACTED" | grep -v "example"`
  - Expected: no matches.

- [ ] **Step 4: Update documentation**
  - Add Cannanas section to `docs/server-voice-quickstart.md`.
  - Write `docs/cannanas-jarvis-integration.md` with architecture, deployment, and usage guide.

- [ ] **Step 5: Final commit**
  - `git add tests/test_acceptance_e2e.go docs/`
  - `git commit -m "docs: Cannanas JARVIS integration acceptance and documentation"`

---

## Execution notes

- Adapter binary must be built and placed at path specified by `ADAPTER_PATH` env var before integration tests run.
- Use mock HTTP server for adapter tests when live API unavailable.
- All new dependencies added to `requirements-headless.txt`.
- If live API access is granted during acceptance, run criterion 18–21 with real key.
- Do NOT modify `/home/lacasa15/services/cannanas-mcp/`; adapter is independent copy.
