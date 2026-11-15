# NEXUS — Rust ↔ Python Interfaces

> **STATUS: INTERFACE SPECIFICATION.** How a Rust performance component is
> called from the Python control plane. No Rust code is created by this
> document.
>
> Evidence classes: **VERIFIED / INFERRED / UNKNOWN / BLOCKED**.

- **Base commit:** `9e01f2f9e088f52bc0d4aeb5b2ac2ae8727885df`
- **Parent:** [HYBRID_PYTHON_RUST_ARCHITECTURE](./HYBRID_PYTHON_RUST_ARCHITECTURE.md)
- **Selection:** [RUST_COMPONENT_SELECTION](./RUST_COMPONENT_SELECTION.md)
- **Roadmap:** [HYBRID_MIGRATION_ROADMAP](./HYBRID_MIGRATION_ROADMAP.md)

---

## 1. Integration mechanism (chosen: PyO3 + maturin)

- **Default:** a Rust crate compiled to a **Python native extension** with
  **PyO3**, built by **maturin** into a wheel. The Python process imports it and
  calls its functions in-process.
- **Why (INFERRED):** no IPC, no serialization of large buffers (the bytes go
  across the PyO3 boundary as a buffer), no second process, no second writer,
  and a trivial fallback (a failed `import` uses the Python implementation).
- **Wheels:** built per platform/ABI. Targets: **Linux x86_64 (cp310–cp313,
  manylinux)**, Windows x86_64, macOS arm64 (dev). Android (Tauri) is out of
  scope for the extension (see §7).
- **GIL:** CPU-heavy functions release the GIL (`py.allow_threads`) so other
  Python threads keep serving requests; a function that needs Python objects
  keeps it.

## 2. When NOT to use a native extension

- A function that calls the network or PostgreSQL → keep Python (no benefit,
  adds a boundary).
- A long-running CPU job that would still block the request path → use a
  **worker process** (the existing `lab_worker.py` pattern) or a sidecar, not an
  in-request extension.
- A function whose measured gain is below the threshold → do not port.

## 3. Contract: errors

One error shape crosses the boundary. The Rust extension raises a Python
exception that maps to the API's existing taxonomy:

| Rust error kind | Python exception | HTTP |
|---|---|---|
| `Validation` | `ValueError` | 400 |
| `NotFound` | `NotFound` (store) | 404 |
| `Refused` (e.g. unsafe archive) | `ArchiveUnsafe` / `ValueError` | 400 |
| `LimitExceeded` | `ValueError` | 400 |
| `Internal` | `RuntimeError` | 500 |

The mapping is asserted by a contract test so a Rust panic never surfaces as an
unhandled traceback: the extension catches panics at the boundary and converts
them to `RuntimeError`.

## 4. Contract: data shapes

- **Structs** are defined once in Rust (`serde`) and mirrored by a Python
  `dataclass`/`TypedDict`; a test pins both to the same JSON.
- **Large payloads** (file bytes) cross as a `bytes`/buffer, never as a
  base64 string.
- **Unicode:** all text is UTF-8; a filename is normalized the same way the
  Python code does (the `archive_safety.normalize_member_name` rules become the
  single source, tested for parity).

## 5. Contract: logging, tracing, metrics

- The extension emits **`tracing`** events bridged to the Python logger; it
  never writes its own log files.
- A **correlation ID** is passed in from Python and echoed; the extension never
  invents one.
- Metrics (duration, bytes, members) are returned in the result struct; Python
  records them through the existing observability path.

## 6. Contract: configuration

- The extension reads the **same environment variable names** as the rest of
  NEXUS (`docs/ENVIRONMENT.md`); it has no config file of its own.
- Limits that already exist in Python (`ArchiveLimits`: `max_entries=2000`,
  `max_total_uncompressed=256 MiB`, `max_depth=16`) are passed in as parameters,
  so the Python side stays the single source of truth for policy.

## 7. Platform matrix

| Platform | Native extension | Notes |
|---|---|---|
| Linux x86_64 (server, Debian) | yes | the primary target; manylinux wheels |
| Windows x86_64 | yes | for the desktop client's local use (later) |
| macOS arm64 | dev only | not a server target |
| Android (Tauri) | no | a Tauri app links Rust directly, not through Python |
| Docker | yes | the wheel is installed in the image (`Dockerfile`) |

## 8. One-writer guarantee (VERIFIED requirement)

A native extension is a **function library**, not a writer: it returns data and
the Python store persists it. Therefore the "one writer per domain" rule
([API_DATABASE_COMPATIBILITY](./API_DATABASE_COMPATIBILITY.md) §5) holds with no
coordination protocol — there is only one process and one writer.

## 9. Parity requirements (must pass before a port is accepted)

- Same inputs → same outputs (byte-for-byte for extraction manifests).
- Same error semantics (§3) and the same limits (§6).
- Same Unicode handling and the same path-traversal/symlink refusals.
- Cancellation/timeout behave the same.
- Deterministic tests; **fuzzing** for the archive parser; **property-based**
  tests for path handling; concurrency tests for the GIL-release path.
- Regression benchmarks with the same scenario as the Python baseline.

## 10. Fallback protocol

1. `try: from nexus_rust import ... except ImportError: use Python`.
2. A feature flag (`NEXUS_USE_RUST_<COMPONENT>`) can force the Python path.
3. The Python implementation is **never deleted** until parity + a stability
   window pass ([HYBRID_MIGRATION_ROADMAP](./HYBRID_MIGRATION_ROADMAP.md) §5).
