# Cloudflare Tunnel for NEXUS

Two ways to give a self-hosted NEXUS a public HTTPS address without opening a
port on your router. Both use Cloudflare Tunnel (`cloudflared`). Official docs:
<https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/> and
<https://developers.cloudflare.com/tunnel/get-started/>.

## What Cloudflare provides, honestly

- **Quick Tunnel** (`trycloudflare.com`) is **free and temporary**. It gives a
  random `https://<random>.trycloudflare.com` address with no account and no
  domain. **The address changes every time you start it.** It is for demos and
  testing, not for a stable deployment.
- **Named Tunnel** gives a **stable** `https://nexus.example.com` address, but it
  needs a **Cloudflare account** and **your own domain** added to Cloudflare.
  Cloudflare does not register a permanent domain for free; you bring the domain.

Neither mode publishes PostgreSQL, the worker or SSH — only the web process is
tunnelled.

## Install cloudflared (Debian)

```bash
# Cloudflare's own package (see the docs for the current command)
sudo apt install cloudflared    # if your Debian ships it
# or the official .deb from https://github.com/cloudflare/cloudflared/releases
```

## Mode 1 — Quick Tunnel (temporary)

```bash
./scripts/nexus.sh tunnel
```

This runs `cloudflared tunnel --url http://127.0.0.1:8080` and prints the public
URL to its output. Press Ctrl-C to stop. The URL is gone when you stop; a new
run makes a new one.

**Streaming:** NEXUS sends answers as one response, not as a stream (see the
Studio limits), so a quick tunnel carries the normal request/response traffic
fine. WebSocket/SSE are not used by the web UI today; if a future feature adds
them, verify the tunnel carries them before relying on it.

## Mode 2 — Named Tunnel (stable)

```bash
# 1. Log in once (opens a browser; do this on a machine with a browser, or use a token)
cloudflared tunnel login

# 2. Create a named tunnel
cloudflared tunnel create nexus

# 3. Route your domain to it
cloudflared tunnel route dns nexus nexus.example.com

# 4. Point it at NEXUS (config file, then run as a service)
#    /etc/cloudflared/config.yml:
#      tunnel: <tunnel-uuid>
#      credentials-file: /root/.cloudflared/<tunnel-uuid>.json
#      ingress:
#        - hostname: nexus.example.com
#          service: http://127.0.0.1:8080
#        - service: http_status:404

# 5. Install as a service (auto-reconnect, starts on boot)
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

The tunnel reconnects automatically. The credential JSON is a **secret**: keep it
`600`, never commit it, and rotate it if it leaks.

## Security when exposing NEXUS

Exposure is only safe if the app's own controls are on. Before you publish:

- **Authentication is mandatory.** NEXUS refuses to bind `0.0.0.0` unless
  `WEB_AUTH_TOKEN` and the three `AUTH_*` variables are set. Set them.
- **HTTPS end to end.** Cloudflare terminates TLS at its edge and forwards to
  your loopback; the loopback hop never leaves the machine.
- **Cookies.** The session cookie is `HttpOnly`, `SameSite=Strict`, and marked
  `Secure` — which is honoured because the browser sees `https://`.
- **Trusted proxy.** Put `TRUSTED_PROXY_HOPS=1` so the login lockout keys on the
  real client IP Cloudflare forwards, not a spoofable header.
- **Rate limits and CSRF** are built in (see `docs/SECURITY_MODEL.md`).

Do **not** publish the app if authentication is not configured. A tunnel makes
whatever is behind it public.

## Verifying a tunnel

```bash
curl -fsS https://<your-url>/healthz     # {"status": "ok"}
curl -fsS https://<your-url>/readyz      # database reachable
```

Then sign in through the browser. If `/healthz` answers but sign-in fails, the
tunnel is fine and the problem is credentials.
