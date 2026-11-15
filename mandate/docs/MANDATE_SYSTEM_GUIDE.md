# Mandate · System Guide for Judges

Agentic Payments Hackathon · Highline Beta, Toronto · 13–15 November 2026

> **The model can be manipulated, but it cannot grant itself purchasing authority.**

## Mandate in one minute

AI agents can now buy things: data, API calls, services. That creates a new kind of risk. An agent that reads a poisoned web page can be talked into paying the wrong party. An agent that retries after a timeout can pay twice. And asking a person to approve every four-cent purchase defeats the point of having an agent.

Mandate is a checkpoint that sits between the agent and the money. The agent can only *ask* to buy something. A separate gateway, which the agent cannot edit or talk to, checks the request against rules the business owner set, sets the money aside, pays through the payment rail, confirms the goods arrived, and writes down every step in a record that shows any later tampering.

The gateway never tries to work out whether the agent has been tricked. It does not need to. A tricked agent still cannot pay a seller that is not on the list, spend above its limit, or approve its own purchase.

## The five stages of a purchase

Every purchase goes through the same five stages, in order. The console shows them left to right on each transaction slip.

| # | Stage | What happens | Who acts | What can go wrong, and what Mandate does |
| --- | --- | --- | --- | --- |
| 1 | **Intent** | The agent proposes a purchase: which seller, which item, what price. | The agent | The agent may have been manipulated. Mandate records the request as-is and treats it as untrusted. |
| 2 | **Policy** | The gateway checks the request against the owner's rules: is the seller on the whitelist at the address on file, is the amount under the cap, are attempts and budget left. | The gateway | A rule fails. The request is halted before any money moves, the exact rule is shown, and the agent's session key is revoked. |
| 3 | **Quote** | The exact amount is frozen and set aside from the budget so no other request can spend it. Anything above the auto-approve limit waits here for a person. | The gateway, and a person for larger amounts | Two requests race for the same money: only one gets it. A person does not answer in time: the hold is released and nothing is paid. |
| 4 | **Settlement** | The gateway writes down that it is about to pay, then pays through the rail. Only after payment is confirmed does it check that the seller delivered. | The rail, then the seller | The rail does not answer: the money stays held and the payment is never blindly re-sent. The seller fails after being paid: the purchase is flagged as paid-but-undelivered and is not charged again. |
| 5 | **Audit** | Every step above is added to a chain of records. Each record contains a fingerprint of the one before it. | Anyone | Someone edits a past record: every fingerprint after it stops matching. Anyone can re-check the whole chain in their browser with one click. |

Amounts are counted in whole thousandths of USD-TDN, the same smallest unit Qatom prices in, so 0.040 is stored as the number 40. There is no rounding anywhere.

## What to watch in the demo

The console has numbered demo keys along the bottom. Three of them tell the whole story in under three minutes.

1. **A routine purchase (0.040).** It is under the limit and the seller is approved, so it settles with no human involved. Watch the slip fill in: reserved, paid, delivered.
2. **A larger purchase (2.500).** It is above the auto-approve limit, so the money is set aside and a review card opens with a countdown. Authorize it and it completes; reject it and the money returns to the budget.
3. **An attack.** The agent has read a web page telling it to pay `attacker.eth`. The gateway halts the request and names the rule: `VENDOR_NOT_WHITELISTED`. Zero payment calls are made, the balance does not move, and the agent's key is revoked. Open the audit pill on that row and press **Verify SHA-256 Chain**.

Then press the **kill-switch**. Every control locks, the agent's key is revoked, and a revocation slip appears at the bottom with its own fingerprint, re-checked on the spot.

## How this maps to what judges asked for

The hackathon asks for a working demo, a public repository, an explanation of the payment flow, and a security and failure-mode analysis. Qatom's judge looks for a payment flow that works on Qatom, real use of spending controls, and how clearly each transaction is shown to be safe and verifiable.

| What is asked | Where Mandate stands today |
| --- | --- |
| Working demo | Yes. The gateway and console run together. The console reads the gateway's live event stream and switches to a built-in simulation if the gateway is offline. |
| Public source repository | Yes. `mandate/` in the team repository, MIT licence. |
| Payment flow explanation | This guide, plus `README.md`. |
| Security and failure-mode analysis | The table below, each row backed by an automated test. |
| Real use of spending controls | Yes: budget, auto-approve limit, hard per-purchase cap, seller whitelist, attempt limit, human approval, kill-switch. All enforced by the gateway, not by the agent. |
| Each transaction safe and verifiable | Yes for the gateway's own record: every step is in the fingerprint chain and can be re-checked in the browser. |
| Payment flow that works **on Qatom** | **Not yet.** Payments in the demo go through a mock rail. See "What is real and what is not". |

## Failure modes, and what happens

| If this happens | Mandate does this | Proven by |
| --- | --- | --- |
| The agent is told to pay an unknown seller | Blocks it, makes zero payment calls, revokes the agent's key | `test_injection_blocks_revokes_and_leaves_wallet_untouched` |
| A known seller's name is used with a different payment address | Blocks it outright; it is never sent for human review | `test_policy_blocks_redirects` |
| Two requests try to spend the same budget at once | One is reserved, the other is refused | `test_concurrent_reservations_cannot_overspend` |
| The payment rail times out | Keeps the money held, refuses a second attempt, waits to look up the original | `test_timeout_keeps_hold_and_forbids_second_order` |
| The rail confirms the same payment twice | Counts it once | `test_timeout_keeps_hold_and_forbids_second_order` |
| The seller takes payment and fails to deliver | Flags it as paid-but-undelivered; a retry of delivery does not charge again | `test_payment_and_delivery_are_separate` |
| Someone edits a past record | The chain check fails at that exact record | `test_evidence_chain_is_append_only_and_tamper_evident` |
| The owner hits the kill-switch | No new payment can start; every agent key is revoked in the same step | `test_kill_switch_revokes_keys_and_stops_dispatch` |
| A bad amount is sent (a float, a negative, too many decimals) | Refused with no effect on the books | `test_invalid_purchase_amount_has_no_ledger_effects` |

Run them all with `python -m pytest -q` from the `mandate/` folder.

## Sponsors and how each one fits

| Sponsor | Role in Mandate | Status |
| --- | --- | --- |
| **Qatom** | The intended payment rail: the agent wallet, per-call payment and approval links. Qatom's own guidance, "a retry is a new charge", is exactly the problem Mandate's pay-once record solves. | Not connected. Qatom is reached through its MCP tools (`catalog_search`, `agent_checkout`); that adapter is ticket MND-01 and MND-04. |
| **GOAT Network** | A second rail, GOAT Flow x402 on Testnet3. | Request signing and the adapter are written and tested against a simulated server. No live call has been made. |
| **Backboard** | Model inference for the buyer agent, using the hackathon inference credits. | Planned for the agent loop (MND-06). Not connected: the console's demo keys stand in for the agent today. |
| **Spur Innovation** | The team's $1,000 credit, intended for upstream agent inference and for hosting the mock supplier APIs. | Planned. Not connected in the repository. |
| Mock supplier APIs | Two fixed-price synthetic items sold by a small seller service that refuses any call without its token. | Built and tested on the `feature/MND-05-controlled-sellers` branch, in review. |

## What is real and what is not

Stated plainly, so nothing in the demo is mistaken for more than it is.

- **Real and tested:** the rule checks, the budget holds, the pay-once record, the separation of payment from delivery, the session keys, the kill-switch, the fingerprint chain, the live event stream and the console.
- **Simulated:** the payment rail. Demo payments go through a mock that behaves like a rail but moves no money.
- **Not built yet:** the Qatom connection, a live GOAT Flow payment, the AI agent itself, and a copy of the chain's latest fingerprint stored outside the database.
- **A limit worth knowing:** the record is tamper-evident, not tamper-proof. An edit to past events is always detectable. Someone who could replace the entire database could also replace the chain; storing a fingerprint elsewhere closes that gap and is the next step.
- **What Mandate does not claim:** it does not detect prompt injection, judge whether purchased data is true, or prevent all fraud. It guarantees that the agent cannot exceed the authority it was given.

## Words used in this guide

- **Agent:** an AI program that carries out a task and may need to buy things to finish it.
- **Gateway:** Mandate's checkpoint. Ordinary code with fixed rules, separate from the AI.
- **Mandate:** the authority an owner grants for one task: which sellers, how much, and when a person must approve.
- **Rail:** the service that actually moves the money, such as Qatom or GOAT Flow.
- **Hold:** money set aside for one purchase so nothing else can spend it.
- **Session key:** the agent's pass to make requests. Revoking it cuts the agent off.
- **Fingerprint (SHA-256 hash):** a short code computed from a record. Change the record and the code changes.
- **USD-TDN:** the currency unit used on the rail. Mandate counts it in thousandths.
