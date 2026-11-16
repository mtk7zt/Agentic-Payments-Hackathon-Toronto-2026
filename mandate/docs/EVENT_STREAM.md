# Authenticated cockpit stream (MND-17)

Set `NEXT_PUBLIC_MANDATE_API_URL` to the gateway origin and configure the gateway's
`MANDATE_OWNER_TOKEN` privately. In the cockpit, enter that credential into the
owner connection form. It is kept only in browser memory and sent in owner REST
request headers. Reloading the tab forgets it. Never add it to a NEXT_PUBLIC
variable, URL, localStorage, sessionStorage, public fixture or repository file.

Use the same hostname for both services (for example localhost on ports 3000 and
8000), or serve both under the same HTTPS site. The gateway's explicit
`MANDATE_CORS_ORIGINS` must include the cockpit origin; wildcard origins are not
supported with credentials. Separate unrelated sites will not receive the
SameSite=Strict cookie. Use HTTPS outside local development.

`POST /api/v1/stream-session` requires the owner header. It returns only a lifetime
and sets a 60-second, HttpOnly, SameSite=Strict cookie scoped to `/api`; HTTPS
requests also set Secure. Its HMAC-SHA256 signature binds a stream-only audience,
mandate ID, agent ID and expiry to the configured owner credential. Rotating or
removing that credential invalidates existing grants, including open streams.
The cookie cannot authorize state, mutations, agent requests or grant renewal.

The browser connects to `/api/events` with cookie credentials; `/api/v1/stream`
is the same handler. CLI clients can still send X-Owner-Token. No secret appears
in a stream URL. Each stream closes after its short lifetime, even when idle.
The client renews through the owner-only endpoint before reconnecting.

Last-Event-ID takes precedence over the `after_seq` query cursor. Cursors are
nonnegative JavaScript-safe integers. Invalid headers return 422. Event IDs are
strictly increasing sequence numbers. A missing successor or cursor beyond the
current head emits `stream.reset` and closes. The cockpit clears its event cache,
refreshes authoritative REST state, and reconnects from zero. Reconnect after an
ordinary disconnect/expiry preserves the cursor. Delivery is read-only and never
settles a purchase or grants approval.

The stream sits on top of the cockpit's 3-second state poll; it does not replace it.
A dropped stream only switches the header back to "polling". One failed poll keeps
the gateway state on screen; two in a row fall back to the labelled simulation. An
auth refusal shows the owner sign-in form at once. The credential typed there is
held in memory only and is gone after a page reload. A configured backend does not establish a live rail
payment; MND-17 makes no claim of provider verification.

This remains the repository's single-tenant, single-agent fixture. Token scope
prevents reuse against another configured mandate/agent; it does not create a
multi-tenant authorization or database isolation system. History is replayed from
the local ledger without retention/compaction support. Cryptographic checkpoint trust is
MND-13. Broader cockpit screen changes and versioned policy UI are separate tickets.

Verification from `mandate`:

```text
python -m pytest backend/tests -q
ruff check backend scripts
```

From `frontend`:

```text
node --test tests/stream.test.cjs
npx tsc --noEmit
npx next build
```
