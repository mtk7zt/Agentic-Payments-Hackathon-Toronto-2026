# Controlled synthetic sellers (MND-05)

The standalone seller service provides two fixed-price demo items. It has no
GOAT credentials and submits no payment. All returned business data is synthetic.

| Item | Price in USD-TDN | Integer milli |
| --- | --- | --- |
| shipping-estimate | 0.040 | 40 |
| supplier-summary | 0.060 | 60 |

`GET /api/v1/catalog` returns item labels, exact prices, synthetic labels and the
strict fulfillment input schema. `POST /api/v1/items/{item}/fulfill` accepts only
`request_id` and `rail_order_id`, both nonempty strings of at most 128 characters.
Extra fields, numeric IDs and client-supplied prices are rejected. Successful
responses contain the item, both correlation IDs, exact price, a UTC ISO timestamp,
the synthetic marker and structured data.

Both endpoints require `Authorization: Bearer <seller token>`. Missing or incorrect
authentication returns 404 before input parsing. With no configured token the
service denies all requests. The bearer token is never included in catalog,
delivery or ledger evidence. There is no public OpenAPI/docs endpoint.

## Local setup

Run commands from the `mandate` directory. Choose a local secret and save it as
`MANDATE_SELLER_TOKEN` in an ignored `.env.seller` file. This file should contain
only the seller credential, not GOAT keys or owner credentials. Start the seller:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.seller_api.main:app --env-file .env.seller --host 127.0.0.1 --port 8001
```

In the gateway's ignored `.env`, set the same `MANDATE_SELLER_TOKEN`,
`MANDATE_SELLER_MODE=http`, and `MANDATE_SELLER_URL=http://127.0.0.1:8001`. Keep
`MANDATE_RAIL=mock` for the credential-independent seller demo. Start the gateway:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.api.main:app --env-file .env --host 127.0.0.1 --port 8000
```

Default seller mode remains `mock`; changing the mode requires restarting the
gateway. HTTP mode refuses to start without a seller token. The adapter uses the
executor-configured URL, refuses redirects, and verifies response correlation.
It is called only after the orchestrator records settlement. Pending, unknown,
unpaid or blocked purchases do not fetch seller data. A delivery failure leaves
payment settled and enters recovery; retrying delivery never creates a new charge.

## Verification and limits

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_controlled_sellers.py
```

The suite exercises both items, authenticated API routes, schema rejection,
correlation, timestamps, redirects, exact money, gateway checkout, replay and
paid-but-undelivered recovery. Checkout tests use a clearly identified mock rail;
they do not establish live payment or independent receipt verification.

The seller trusts the executor's bearer capability. `rail_order_id` is a
correlation reference, not a verified proof of payment. The service does not
independently inspect a provider receipt. Catalog price binding to a purchase is
not enforced by this delivery adapter; the caller must propose the exact catalog
price. Full pre-dispatch argument/price constraints belong to MND-07/MND-10.
MND-12 validates delivery age and bounded future clock skew; seller timestamps
are still untrusted declarations rather than independent proofs of freshness.
Process deployment and secret isolation remain MND-03 work. No live supplier
selection, automated refunds or real business decisions are supported.
