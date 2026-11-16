# NEXUS — Clients Architecture

> **STATUS: SPECIFICATION.** Shared contracts for the Web, Desktop, Android and
> CLI clients. Desktop and Android are **not** assumed to have equal
> capabilities.
>
> Evidence classes: **VERIFIED / INFERRED / UNKNOWN / BLOCKED**.

Parent: [FINAL_ARCHITECTURE_DECISION](./FINAL_ARCHITECTURE_DECISION.md).
Related: [API_DATABASE_COMPATIBILITY](./API_DATABASE_COMPATIBILITY.md).

---

## 1. What exists today (VERIFIED)

| Client | Today | Evidence |
|---|---|---|
| Web | studio: static HTML/CSS/JS, no build step | `orchestration/studio/` |
| PWA | service worker + manifest | `orchestration/pwa/` |
| CLI | Python `cli.py` (bearer JSON client) | `orchestration/cli.py` |
| MCP | server (stdio + HTTP) | `orchestration/mcp_server.py` |
| Desktop | **not implemented** (a `.desktop` file + a `nexus-client.py` stub) | `packaging/debian/` |
| Android | **not implemented** (an `AndroidManifest.xml` + a `MainActivity.java` + an APK build script) | `packaging/android/` |
| Windows | **not implemented** (a C stub + an exe build script) | `packaging/windows/` |
| iOS | **not implemented** | — |

So "clients" today = Web + CLI + MCP. Desktop/Android are packaging stubs only.

---

## 2. Shared contract (all clients)

- **Authentication:** browser → session (PBKDF2 login, server-side session,
  double-submit CSRF); machine → bearer (`WEB_AUTH_TOKEN`).
- **Authorization:** the server enforces membership; a client shows only what
  the server returns.
- **Data model:** projects → conversations → messages; runs; files. No client
  invents state the server does not have.
- **API:** the same HTTP surface; no client-specific endpoints.

---

## 3. Web (VERIFIED baseline; target = a full platform)

- Today: one screen, chat-first, tabs for Agent/Files, 7 composer modes.
- Target: full CRUD + UI parity for every applicable entity
  ([PYTHON_RUST_MIGRATION_MATRIX](./PYTHON_RUST_MIGRATION_MATRIX.md) §5) —
  including the 9 code-only surfaces that need a screen.
- React + TypeScript + the existing design system (after a design audit).
- **Not a demo set of screens** — the acceptance criterion is a complete
  workflow per entity.

---

## 4. Desktop (Tauri 2 — primary candidate)

| Concern | Requirement | Status |
|---|---|---|
| Local filesystem | open/save, drag-drop into File Lab | to verify in a spike |
| Terminal | an embedded terminal is a **new** capability | UNKNOWN |
| Permissions | OS file/network permission model | to verify |
| Update mechanism | signed auto-update | to verify |
| Packaging | `.deb`, AppImage, `.exe`, `.msi` | build scripts exist as stubs |
| Security | token in OS keychain, not a plain file | to verify |

**Tauri 2 is the primary candidate but is not accepted until a spike proves
filesystem, terminal, permissions, update and packaging.**

---

## 5. Android (Tauri 2 — to be evaluated, not assumed)

| Concern | Risk | Status |
|---|---|---|
| Tauri 2 on Android | WebView + Rust; real capability? | **to verify with a spike** |
| Background restrictions | Android kills background work; the orchestrator loop cannot live on the phone | design: the server owns the loop |
| Notifications | FCM/push needed; not the web long-poll | to design |
| Filesystem | scoped storage limits | to verify |
| Secure storage | Keystore-backed, not a file | to verify |
| Lifecycle | process death, doze | design: server-authoritative |
| Realtime | push vs polling | to design |

**Decision rule:** if Tauri 2 cannot meet notifications + secure storage +
lifecycle on Android, use an alternative (Kotlin/JNI or a thin WebView shell).
Android and Desktop are explicitly **not** the same capability set.

---

## 6. CLI (target: a Rust binary)

- Shared auth (bearer), API, project context, event handling.
- Machine-readable output + stable exit codes (as the Python `cli.py` already
  does: `EXIT = {…}`).
- Owns no state; a thin client over the same API.

---

## 7. Per-client matrix

| Concern | Web | Desktop | Android | CLI |
|---|---|---|---|---|
| Auth | session+CSRF | session or bearer | bearer + Keystore | bearer |
| Authorization | server | server | server | server |
| Sync | polling (1s/5s) | polling | push/polling | polling |
| Offline | no | read cache (design) | read cache (design) | no |
| Reconnect | reload | retry | retry | retry |
| Notifications | in-app | OS | push | none |
| File access | upload/download | full FS | scoped | paths |
| Security | CSRF, secure cookie | keychain | Keystore | env token |
| Packaging | static | `.deb`/AppImage/`.exe`/`.msi` | APK/AAB | binary |
| Tests | `test_browser_e2e` | build test | build test | `test_cli_mcp` |

---

## 8. Acceptance

A client is accepted when a user can complete the full workflow for every
applicable entity on it, with the same authorization as the web client, and its
packaging artefact builds in CI. Today only Web and CLI meet this; Desktop and
Android are **not implemented** and are out of scope until a spike proves the
platform capabilities.
