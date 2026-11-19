# Deploying Mandate

Mandate runs as two small services from one image:

- **Gateway** (`backend.api.main:app`, port 8000): the API agents and the console call.
- **Seller** (`backend.seller_api.main:app`, port 8001): the delivery check. It is never exposed
  to the internet; only the gateway talks to it.

The database is PostgreSQL. The compose file can run PostgreSQL 16 on the same server (see
below), or you can point `DATABASE_URL` at a hosted server such as Supabase. For Supabase, use
the session pooler URI with the `mandate_gateway` login. Apply or upgrade the schema with
`py -m backend.migrate`. The gateway also applies any missing migration when it starts.

Pick one of the two routes below. Both use the `Dockerfile` in `mandate/`.

## Bundled Postgres

`deploy/docker-compose.yml` runs `postgres:16` next to the gateway. Port 5432 is not published
on the host. Leave `DATABASE_URL` empty and set `POSTGRES_PASSWORD`, `MANDATE_DB_PASSWORD`
and `MANDATE_READONLY_PASSWORD`. Compose then connects as

`postgresql://mandate_gateway:<MANDATE_DB_PASSWORD>@postgres:5432/mandate`.

`POSTGRES_PASSWORD` is only the database superuser. The gateway process does not receive it.
Make each password with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
`MANDATE_DB_PASSWORD` is copied into the gateway URL, so keep it to that alphabet.

Data goes to a named volume. To put it on the VM data disk instead, set
`MANDATE_PGDATA=/srv/mandate/postgres`, create that directory, and give it to the Postgres
user in the official image (uid 999):

```bash
sudo mkdir -p /srv/mandate/postgres
sudo chown 999:999 /srv/mandate/postgres
```

The first start creates the database `mandate`, owned by `mandate_gateway`, and a
`mandate_readonly` login that can only `SELECT`. That login is for a later read-only client
such as pgweb. It is not the role the gateway uses.

Once Postgres is healthy, compose runs `python -m backend.migrate` as `mandate_gateway`, then
starts the gateway. The gateway runs any migration that is still missing. Because
`mandate_gateway` creates the tables, it owns them. That is what the PostgreSQL lockdown
expects: row-level security is on and public grants are revoked, and the table owner still
reads and writes. `mandate_readonly` bypasses row-level security because that lockdown has no
policies and would otherwise return no rows; it has no write grants, and it is only reached
later by pgweb over IAP.

The server is a 4 GiB VM. Postgres is given `shared_buffers` of 512MB, `work_mem` of 4MB and
`max_connections` of 40, leaving the rest of the memory for the gateway, the seller and the OS.

Nightly backups and the restore drill are in `docs/BACKUPS.md`.

To keep an external database, set `DATABASE_URL` to the full URI. The migrate job and the
gateway use that value and do not replace it. This compose file still starts the local
Postgres, so the three passwords above are still required. On a managed host that does not use
this compose file, only `DATABASE_URL` matters (route B).

## DNS

The production value of `MANDATE_DOMAIN` is `api.mandatepay.tech`. Caddy reads `$MANDATE_DOMAIN`
from the environment. The Caddyfile does not hard-code the name.

At GoDaddy, open DNS for `mandatepay.tech` and add an A record:

| Host | Type | Value |
| --- | --- | --- |
| `api` | A | the static external IP of `mandate-prod` |

Caddy requests a certificate once that name resolves to the VM. Check:

```bash
nslookup api.mandatepay.tech
curl -fsS https://api.mandatepay.tech/live
curl -fsS https://api.mandatepay.tech/ready
```

`nslookup` should print that same IP. `/live` returns `{"live":true}` and `/ready` returns
`{"ready":true}`.

## Route A: one VPS (DigitalOcean, Hetzner, Vultr, Lightsail, Azure VM)

You need an Ubuntu 22.04 or 24.04 server, a domain or subdomain for the API (for example
`api.mandatepay.tech`), and SSH access. The bundled Postgres wants about 4 GB of RAM on the
machine. An external database can get by with less.

1. **Point the domain at the server.** Add the GoDaddy A record described under DNS above.
   Caddy cannot issue the HTTPS certificate until `api.mandatepay.tech` resolves.
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
   MANDATE_CORS_ORIGINS=http://localhost:3000
   MANDATE_DOMAIN=api.mandatepay.tech
   POSTGRES_PASSWORD=<url-safe random string>
   MANDATE_DB_PASSWORD=<a different url-safe random string>
   MANDATE_READONLY_PASSWORD=<a third random string>
   ```

   For the bundled database, leave `DATABASE_URL` empty. For an external one, set `DATABASE_URL`
   to the URI in the example above. `MANDATE_CORS_ORIGINS` is a comma-separated list of exact
   origins. Spaces around the commas are ignored, and so are empty entries. One origin still
   works. `*` is refused. Leave the value at `http://localhost:3000` until the console exists.
   Later it can be `https://app.mandatepay.tech,https://market.mandatepay.tech`.
   Make each token with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
   Then `chmod 600 .env`.
6. **Give the gateway the Qatom sign-in.** Sign in on your own machine (`docs/rails/QATOM.md`),
   add that file as a version of the secret `mandate-qatom-token`, then on the VM run
   `sudo deploy/secrets/fetch-qatom-token.sh` before `docker compose up`. The token is a file,
   never an environment value, and `deploy/secrets/` stays out of git. Renewal is in that same
   doc. The VM needs outbound HTTPS on port 443 to `mcp.m.todaq.net` (allowed by default).
7. **Start everything:**

   ```bash
   cd deploy
   docker compose --env-file ../.env up -d --build
   ```

8. **Check it:** `https://api.mandatepay.tech/live` returns `{"live":true}` and
   `https://api.mandatepay.tech/ready` returns `{"ready":true}`.

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

## Admin access

File Browser and Portainer stay off until you pass `--profile admin`. pgweb stays off until
you pass `--profile dbadmin`. The public Caddy site is unchanged. The admin proxy listens on
`127.0.0.1:8443` by default. `start-iap-tunnel` connects to the VM's internal IP, so a
listener on `127.0.0.1` is not reachable that way. Set `ADMIN_BIND` to that internal IP,
for example `10.128.0.2`. Never set `ADMIN_BIND` to `0.0.0.0`.

File Browser is rooted at `/srv/mandate` (or `MANDATE_ADMIN_ROOT`). It has no Docker socket.
Set `FILEBROWSER_PASSWORD` before the first start. The password is stored then; it is not
`admin`. Portainer has the Docker socket, so it is root-equivalent. It asks you to create
the admin user on first visit. There is no default password. `PORTAINER_TRUSTED_ORIGINS`
is a comma-separated list of bare hosts, with no scheme and no port. The default is
`127.0.0.1`. pgweb connects as
`mandate_readonly` and should be stopped when you are finished. Keep that password URL-safe;
it is placed in the connection string.

```bash
cd deploy
docker compose --env-file ../.env --profile admin up -d
docker compose --env-file ../.env stop filebrowser portainer caddy-admin

docker compose --env-file ../.env --profile dbadmin up -d
docker compose --env-file ../.env stop pgweb caddy-admin
```

Cockpit is installed on the host, not in Compose. The script does not open the firewall:

```bash
sudo COCKPIT_BIND=127.0.0.1 deploy/host/install-cockpit.sh
sudo systemctl stop cockpit.socket
```

Reach these through IAP, not the public address. The firewall should allow `35.235.240.0/20`
only on ports 22, 8443 and 9090.

```bash
gcloud compute start-iap-tunnel mandate-prod 8443 --local-host-port=localhost:8443 --zone=us-central1-a
gcloud compute start-iap-tunnel mandate-prod 9090 --local-host-port=localhost:9090 --zone=us-central1-a
gcloud compute ssh mandate-prod --tunnel-through-iap --zone=us-central1-a
```

Then open `http://127.0.0.1:8443/files`, `http://127.0.0.1:8443/portainer/`, and, while pgweb
is running, `http://127.0.0.1:8443/pgweb`. Cockpit is `https://127.0.0.1:9090`.

## Production VM on Google Cloud

GitHub Actions does not create the VM. The deploy account cannot create addresses or
firewall rules. From Cloud Shell, as the project owner, in a checkout of this repo:

```bash
bash mandate/deploy/gcp/create-server.sh
```

Run it again any time. It creates what is missing and leaves what is already there.
It does not change IAM and it does not print secret values. It reserves the regional
address `mandate-prod-ip`, opens 80 and 443 to the internet on the tag `mandate-web`,
and opens 22, 8443 and 9090 only from `35.235.240.0/20`. It does not open 5432, 8000
or 8001. It deletes `default-allow-ssh` and `default-allow-rdp` when they exist,
because those rules open ports to the internet. The VM is `e2-medium`, Ubuntu 24.04,
Shielded VM, OS Login, a 20 GB boot disk and a 10 GB data disk. Both disks get a
daily snapshot kept for 14 days.

The script prints the static IP. In GoDaddy, add the A record from the DNS section
above: host `api`, value that IP.

On every boot the startup script formats the data disk only when it has no filesystem,
mounts it at `/srv/mandate` by UUID, adds a 2 GB swap file, installs Docker Engine and
the Compose plugin from Docker's apt repository, and clones this repo to
`/srv/mandate/app` if that checkout is missing. The OS Login user is not in
`google-sudoers`, so the deploy workflow runs the remote step with sudo.

It writes `/srv/mandate/app/mandate/.env` mode 600 from Secret Manager, using the
runtime service account. A secret still set to `placeholder-change-me` is skipped.
The log line names the secret and not the value.

| Secret | Written as |
| --- | --- |
| `mandate-database-url` | `DATABASE_URL`. Leave the placeholder to use Postgres on this VM (the file gets an empty `DATABASE_URL` and the wrapper builds the URL). |
| `mandate-postgres-password` | `POSTGRES_PASSWORD` |
| `mandate-db-password` | `MANDATE_DB_PASSWORD`. The bootstrap does not create this secret. Add it before the first deploy. |
| `mandate-readonly-password` | `MANDATE_READONLY_PASSWORD`. Same. |
| `mandate-owner-token` | `MANDATE_OWNER_TOKEN` |
| `mandate-checkpoint-key` | `MANDATE_CHECKPOINT_KEY` |
| `mandate-seller-token` | `MANDATE_SELLER_TOKEN` |
| `mandate-goat-flow-api-key` | `GOAT_FLOW_API_KEY` |
| `mandate-qatom-token` | `deploy/secrets/qatom-token.json`, mode 600, owned by uid 10001. Not an environment variable. |
| `mandate-backup-encryption-key` | `AGE_RECIPIENT` in `/etc/mandate/backup.env`. The `age1` public key only. A private key is not written. |

`MANDATE_MODE=production`, `MANDATE_RAIL=qatom`, `MANDATE_SELLER_MODE=http`,
`MANDATE_SELLER_URL=http://seller:8001`, `MANDATE_DOMAIN=api.mandatepay.tech` and
`MANDATE_PGDATA=/srv/mandate/postgres` are written as plain values. Make each password
with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`, a different value
each time. `MANDATE_DB_PASSWORD` has to stay in that alphabet. Reboot the VM after the
secrets are real so startup writes `.env` again.

Set the repository variables to the block the script printed.
`GCP_WIF_PROVIDER` is the full provider name from the bootstrap. This script does not
create it. Then in GitHub open Actions, select the deploy workflow, and use Run workflow
on `main`.

The backup bucket should not be public:

```bash
gcloud storage buckets describe gs://mandate-515200-mandate-backups --format="value(public_access_prevention)"
```

That should print `enforced`.

## Deploy from GitHub

`.github/workflows/deploy.yml` runs on a push to `main` and when someone starts it by hand.
It does not run on a pull request. The job uses the `production` environment. It skips itself,
and the run stays green, until these repository variables are set (`vars`, not secrets):

| Variable | What it is |
| --- | --- |
| `GCP_PROJECT_ID` | Google Cloud project id |
| `GCP_WIF_PROVIDER` | Full Workload Identity provider name |
| `GCP_DEPLOY_SA` | Deploy service account email |
| `GCE_INSTANCE` | VM name, `mandate-prod` |
| `GCE_ZONE` | VM zone, for example `us-central1-a` |
| `GCE_DEPLOY_DIR` | Optional. Checkout on the VM. Default `/srv/mandate/app` |
| `GAR_LOCATION` | Artifact Registry region, for example `us-central1` |
| `GAR_REPOSITORY` | Artifact Registry repository name |

The Workload Identity provider must allow only this repository on `main`:

```
assertion.repository == 'mtk7zt/Agentic-Payments-Hackathon-Toronto-2026' && assertion.ref == 'refs/heads/main'
```

The deploy account (`mandate-deploy`) needs `roles/compute.instanceAdmin.v1` or
`roles/compute.osAdminLogin`, plus `roles/iap.tunnelResourceAccessor`,
`roles/iam.serviceAccountUser` on the VM's runtime service account, and
`roles/artifactregistry.writer`. The VM's runtime service account needs
`roles/artifactregistry.reader` so it can pull the image. No service-account key is stored
in the repo or in Actions secrets. The workflow does not set
`NEXT_PUBLIC_MANDATE_OWNER_TOKEN` and does not print the server's `.env`.

The job builds `mandate/Dockerfile` and pushes
`<location>-docker.pkg.dev/<project>/<repository>/mandate` with the commit SHA and `latest`.
The server must already have a git checkout at `GCE_DEPLOY_DIR` and the production
`.env`. Over IAP SSH the job runs the remote step with sudo: it checks out that
commit as root and runs `docker compose pull && docker compose up -d` for the default profile only.
`MANDATE_IMAGE` points at the SHA tag. An empty `MANDATE_IMAGE` still builds locally as
`mandate:latest`. The compose file runs the migrate one-shot, and the job waits until
`/ready` succeeds.

To roll back, start the workflow on `main` and pass the older commit as `sha`, or re-run an
earlier successful deploy. The server checks out that commit and brings the stack up again.

## Scaling and limits

- Run **one gateway process** for now. Idempotency is shared through the database, but the rate
  limiter is per process (see `docs/OPERATIONS.md`); more replicas multiply the effective limit.
- The ledger serialises writes with a PostgreSQL advisory lock, so a second replica is safe for
  correctness when you do add one.
- Keep `MANDATE_CORS_ORIGINS` to exact origins, separated by commas. `*` is refused at start-up.
  Until the console exists, leave the list at `http://localhost:3000`.

## Before calling a deployment done

- `/ready` is green, and a $0.001 live purchase (`scripts.qatom_live`) settles against the hosted
  database with `evidence chain intact: True`.
- `.env` and the Qatom token are readable only by their owner on the server.
- The seller port is not reachable from the internet (`curl https://api.mandatepay.tech:8001` fails).
- Postgres is not reachable from the internet either (port 5432 is not published).
- Ports 8443 and 9090 are not reachable on the public address.
