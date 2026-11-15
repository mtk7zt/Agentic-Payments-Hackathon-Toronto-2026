# Bounded buyer agent (MND-06)

`backend.agent.runner` supplies a single-use buyer loop and an HTTP gateway client.
The model callback receives prior tool observations and remaining timeout seconds,
then returns a tool action or `None` to finish. No model SDK, inference credit or
payment secret is needed for deterministic testing. A provider-specific inference
adapter (such as Backboard) remains a separate integration.

The only model-facing tools are:

| Tool | Gateway route | Capability |
| --- | --- | --- |
| discover | GET /api/v1/catalog | Synthetic items, exact prices, allowed sellers and mock/live rail label |
| propose | POST /api/v1/purchases | Ask the gateway to evaluate an exact purchase |
| status | GET /api/v1/purchases/{purchase_id} | Read purchase status using an active session |

No arbitrary URL, owner token, shell, approval, wallet or direct checkout tool is
exposed. `AgentGateway` takes a session key explicitly, reads no environment
credentials, removes inherited owner/auth/cookie headers and refuses redirects.
The gateway still owns policy evaluation, holds, dispatch and settlement.

## Limits and uncertain outcomes

`RunLimits()` defaults to 20 model steps, 60 seconds elapsed, and five distinct
proposal request keys. Limits are supplied by the application, not model output.
Every model invocation consumes one step. A replay of an existing request key
does not consume another new-attempt slot, but still consumes a model step. Failed
new requests count conservatively as attempts. Setting max_new_attempts to zero
allows discovery/status only. A runner cannot be restarted to reset its counters.

Model and tool calls run behind a deadline. A late model result is discarded
without dispatch. A timeout or tool error stops the run without automatic retry;
the original uncertain request key is retained in the result. An HTTP request
already sent may still complete at the gateway. Inspect/replay the original exact
request through the durable gateway rather than issuing a new purchase key.
Inference adapters should also honor the timeout passed to the model callback;
a timed-out background inference may continue until its own provider timeout.

These are limits for one process-local task invocation, not distributed or
restart-proof agent quotas. A newly constructed runner represents a new run;
durable gateway budget/dispatch records continue to apply across runs. Hard
process and credential isolation remain MND-03 work.

## Durable intent identity

New v2 fingerprints bind mandate, agent, normalized seller domain/address, item,
integer amount and the complete JSON tool call, including arguments. JSON object
key order is irrelevant. A repeated key with changed arguments returns HTTP 409
without any new event, hold or payment.

Older stored purchases replay by comparing their stored immutable fields and
tool-call JSON. Their original fingerprints and dispatch claims are never rewritten.
The SQLite schema remains unchanged. This fixture supports one buyer/mandate;
it does not implement multi-tenant storage or global request-key namespaces.

## Owner configuration and verification

Set `MANDATE_OWNER_TOKEN` in the gateway environment. Owner routes fail closed
when it is missing, including session issuance, policy/approval/pause controls,
state, evidence and SSE. Send it only as `X-Owner-Token` from the owner client.
The agent receives only `X-Session-Key`. The simulated cockpit still works without
an API URL; backend owner actions now require a configured token. Native browser
EventSource cannot attach this header; browser event authentication remains MND-17.

From the repository root:

```powershell
.\mandate\.venv\Scripts\python.exe -m pytest .\mandate\backend\tests\test_agent_intents.py .\mandate\backend\tests\test_agent_runner.py -q
```

The tests cover stored argument replay, legacy records, owner-route denial,
authenticated discovery/status, tool allowlisting, step/time/attempt limits,
late model output, credential stripping and uncertain-request retention. No live
model inference or payment is claimed by these tests.
