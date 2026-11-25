# Self-QA execution modes

Self-QA runs commands declared by the repository. **A temporary checkout is not
a security sandbox.** The existing default `NEXUS_SELFQA_RUNTIME=host` executes
those commands with the worker's operating-system permissions and access to its
filesystem and network, despite a filtered environment. Use that mode only for
trusted code, on a dedicated worker. Process-group cleanup and bounded output
reduce resource risks but do not prevent a hostile process escaping its group.

## Optional Docker runner

`NEXUS_SELFQA_RUNTIME=docker` uses a locally preloaded, operator-controlled image
identified by `NEXUS_SELFQA_IMAGE`. Set the latter to an immutable Docker image
ID `sha256:<64 lowercase hex>` or an image reference with a SHA-256 digest. The
image must already contain every required tool and dependency; NEXUS does not
pull images or install dependencies at check time. For example, after building
and auditing a Python image that includes the appropriate pytest version:

```bash
docker image inspect --format '{{.Id}}' YOUR_TRUSTED_SELFQA_IMAGE
# Set NEXUS_SELFQA_IMAGE to the resulting sha256:... value in the worker env.
```

The image, its base and dependencies must be vetted and built separately. Node
and Jest require an image that includes them. No such image is shipped or
validated by this change. Apply these settings to the process that runs the lab
worker (including the web process when `NEXUS_LAB_INLINE` is enabled). The
worker must be a non-root POSIX user with access to the **local** Docker Unix
socket. `NEXUS_SELFQA_DOCKER_SOCKET` defaults to `/var/run/docker.sock`; set it
to an absolute local Unix socket path for another local daemon. There is no
remote TCP Docker option.

Each declared check gets a separate container with no network, no capabilities,
no-new-privileges, a read-only root filesystem, a read-only checkout, a 64 MiB
`/tmp` tmpfs, 512 MiB memory, one CPU, 128 PIDs, and the worker's non-root UID
and GID. Clone metadata `.git` is removed after the checked commit is verified;
checks that require it or need to write to the checkout must be adapted. The
container uses no host credential environment, and output is bounded and
redacted. `--pull=never` prevents implicit pulls. A timeout triggers explicit
container removal; if that cannot be confirmed, the gate reports blocked. A
worker crash or forced termination can still leave a container alive: monitor
`docker ps --filter label=nexus.selfqa.managed=true` and clean up stale
`nexus-qa-*` containers. Do not blindly delete active checks from other workers.

Docker/image/daemon errors, invalid runtime configuration and failed cleanup
**block verification**; they do not fall back to running code on the host. The
existing quality gate attaches the check receipt to task evidence and signs it
when the evidence signer is configured; successful receipts include the runtime
and, for Docker, image digest. The current agent API does **not** expose a
`run_tests_in_sandbox` tool.

## Security and verification limits

Containers share a kernel and are not a strong boundary against a hostile kernel
exploit. Docker-socket access is highly privileged; never expose the socket to a
container or an agent. Use a dedicated VM/daemon for hostile repositories, apply
host-level disk/process/network controls and monitor orphaned containers. The
read-only checkout and offline network can make existing checks fail; such a
failure is not silently reported as a pass. User-namespace/rootless Docker and
filesystem permissions vary: verify the configured UID can read the bind mount.
The CLI and daemon must be on the same machine and see the same checkout path.

The automated tests use a stand-in for the Docker process and verify command
construction, failure handling and the signed receipt path; **they do not prove
real Docker isolation**. An opt-in integration test exists:

```bash
NEXUS_TEST_DOCKER_IMAGE='sha256:<preloaded-python-image-id>' \
  python -m pytest tests/test_selfqa_sandbox.py -q
```

That test requires a trusted local daemon, a non-root worker, and a Python image;
it checks non-root execution, read-only checkout, no Docker socket in the
container and only the loopback network interface. It is skipped if the image
is not supplied. The `selfqa-container` CI job supplies a Python fixture; its
result and the target deployment must be checked before enabling the mode.
