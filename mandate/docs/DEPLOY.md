# Deploying Mandate

Mandate runs as two small services from one image:

- **Gateway** (`backend.api.main:app`, port 8000): the API agents and the console call.
- **Seller** (`backend.seller_api.main:app`, port 8001): the delivery check. It is never exposed
  to the internet; only the gateway talks to it.

The database is PostgreSQL. Supabase is supported as is: use the session pooler URI with the
`mandate_gateway` login. Before the first deploy, run `py -m backend.migrate` once from your own
machine; the gateway also applies any missing migration when it starts.

Pick one of the two routes below. Both use the `Dockerfile` in `mandate/`.

## Route A: one VPS (DigitalOcean, Hetzner, Vultr, Lightsail, Azure VM)

You need an Ubuntu 22.04 or 24.04 server with at least 1 GB of RAM, a domain or subdomain for the
API (for example `api.yourdomain.com`), and SSH access.

1. **Point the domain at the server.** Add a DNS `A` record for `api.yourdomain.com` with the
   server's public IP. Caddy cannot issue the HTTPS certificate until this resolves.
2. **Install Docker** on the server:

   ```bash
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER   # then log out and back in
   ```

3. **Open only the web ports:**

   ```bash
   sudo ufw allow OpenSSH && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable
   ```

4. **Get the code:**

   ```bash
   git clone https://github.com/mtk7zt/Agentic-Payments-Hackathon-Toronto-2026.git
   cd Agentic-Payments-Hackathon-Toronto-2026/mandate
   ```

5. **Create `.env`** in `mandate/` from `.env.example`. Never commit it. At minimum:

   ```
   MANDATE_MODE=production
   DATABASE_URL=postgresql://mandate_gateway.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
   MANDATE_RAIL=qatom
   MANDATE_SELLER_MODE=http
   MANDATE_OWNER_TOKEN=<43-character random string>
   MANDATE_SELLER_TOKEN=<a different random string>
   MANDATE_CORS_ORIGINS=https://your-console-domain
   MANDATE_DOMAIN=api.yourdomain.com
   ```

   Make each token with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
   Then `chmod 600 .env`.
6. **Give the gateway the Qatom sign-in.** Run `py -m scripts.qatom_login` on your own machine,
   then copy the token file (`~/.toda-mcp-token.json`) to the server as
   `mandate/deploy/secrets/qatom-token.json` and `chmod 600` it. The `secrets/` folder is ignored
   by git. The token lasts about 3 days; repeat this step when it expires.
7. **Start everything:**

   ```bash
   cd deploy
   docker compose --env-file ../.env up -d --build
   ```

8. **Check it:** `https://api.yourdomain.com/live` returns `{"live":true}` and
   `https://api.yourdomain.com/ready` returns `{"ready":true}`.

Updating later: `git pull`, then `docker compose --env-file ../.env up -d --build` again.
Logs: `docker compose logs -f gateway`. The gateway writes one JSON line per request.

What you own on a VPS: operating-system updates (`sudo apt upgrade` monthly), watching disk
space, and renewing the Qatom token. Caddy renews the HTTPS certificate by itself.

## Route B: a managed host (Railway, Render, Fly.io, Azure Container Apps)

No server to maintain. Create **two services** from this repository, both built from
`mandate/Dockerfile`:

| Service | Start command | Public? | Environment |
| --- | --- | --- | --- |
| gateway | (image default) | Yes, HTTPS on port 8000 | everything from the `.env` list above, plus `MANDATE_SELLER_URL=<the seller's private address>` and `MANDATE_TRUST_PROXY_HEADERS=1` |
| seller | `uvicorn backend.seller_api.main:app --host 0.0.0.0 --port 8001` | **No**, private network only | `MANDATE_SELLER_TOKEN` only |

`MANDATE_DOMAIN` is not needed: the host provides HTTPS. For the Qatom token, use the host's
secret-file feature mounted at `/home/mandate/.qatom/token.json` and set
`QATOM_TOKEN_FILE=/home/mandate/.qatom/token.json`.
Point the health check at `/live` and the readiness check at `/ready`.

## Monitoring with Datadog (optional)

The gateway already writes one JSON log line per request and serves `/live` and `/ready`.
The compose file has a Datadog agent behind the `monitoring` profile; it stays off until you
turn it on.

1. In Datadog, go to Organization Settings → API Keys and copy a key. Add it to `.env` as
   `DD_API_KEY=...`. If your Datadog account is not on `datadoghq.com` (for example `us5.datadoghq.com`),
   set `DD_SITE` too.
2. Start with the profile:

   ```bash
   docker compose --env-file ../.env --profile monitoring up -d
   ```

3. In Datadog, check **Logs** for `service:mandate-gateway` and **Infrastructure** for the host.

Suggested monitors (Datadog → Monitors → New):

| Monitor | Condition | Why |
| --- | --- | --- |
| Gateway not ready | the `mandate-ready` HTTP check failing for 2 minutes | The database or the gateway is down |
| Server errors | access-log count `service:mandate-gateway @status:>=500` above 5 in 5 minutes | Unhandled errors reach users |
| Throttling | access-log count `@status:429` above 50 in 5 minutes | An agent is looping or someone is probing |
| Slow requests | access-log `@duration_ms` p95 above 2000 for 10 minutes | Database or rail latency |

Purchase outcomes (a rail refusal, an unknown outcome waiting for you) are recorded in the
evidence log in the database, not in the request logs, so these monitors do not see them yet.
Watching them needs a log line per evidence event, which is a separate change.

Logs never contain tokens, headers or request bodies (see `docs/OPERATIONS.md`), so they are
safe to send. Only containers labelled for Datadog are collected; the seller and Caddy are not.

## Scaling and limits

- Run **one gateway process** for now. Idempotency is shared through the database, but the rate
  limiter is per process (see `docs/OPERATIONS.md`); more replicas multiply the effective limit.
- The ledger serialises writes with a PostgreSQL advisory lock, so a second replica is safe for
  correctness when you do add one.
- Keep `MANDATE_CORS_ORIGINS` to the console's exact origin. `*` is refused at start-up.

## Before calling a deployment done

- `/ready` is green, and a $0.001 live purchase (`scripts.qatom_live`) settles against the hosted
  database with `evidence chain intact: True`.
- `.env` and the Qatom token are readable only by their owner on the server.
- The seller port is not reachable from the internet (`curl https://api.yourdomain.com:8001` fails).
