/**
 * Shared Mandate contracts. Money is integer thousandths of USD-TDN internally:
 * 1 milli = 0.001 USD-TDN. Milli values must be safe integers; purchase amounts
 * must also be positive. A TypeScript number alias does not validate runtime data.
 *
 * Purchase request/response JSON uses decimal strings (e.g. "0.040"); explicitly
 * named amount_milli fields and event amounts use integer milli values. GOAT's
 * amount_wei uses an integer string: 40 milli -> "40000" for a 6-decimal token.
 * Keep provider token base units as strings rather than JavaScript numbers.
 */

// Simulated cockpit contracts. These retain their existing UI names and shapes.
export type Milli = number;

export type Stage =
  | "PROPOSED"
  | "PENDING_APPROVAL"
  | "RESERVED"
  | "DISPATCHED"
  | "SETTLED" // payment settled, delivery not yet verified
  | "VERIFIED" // payment settled and delivery verified
  | "BLOCKED"
  | "REJECTED"
  | "EXPIRED"
  | "RECOVERY";

export type Decision = "AUTO_APPROVED" | "HELD_FOR_REVIEW" | "BLOCKED";
export type Clause =
  | "WITHIN_AUTO_APPROVE_LIMIT"
  | "ABOVE_AUTO_APPROVE_LIMIT"
  | "VENDOR_NOT_WHITELISTED"
  | "VENDOR_UNLISTED_NEEDS_REVIEW"
  | "ABOVE_TRANSACTION_CEILING"
  | "RETRY_CEILING_REACHED"
  | "INSUFFICIENT_ALLOCATION"
  | "AGENT_FROZEN";

export interface Policy {
  version: number;
  autoApprove: Milli;
  txCeiling: Milli;
  strict: boolean;
  vendors: string[];
  maxRetries: number;
  paused: boolean;
}

export type ScenarioKey = "micro" | "escalation" | "injection" | "sellerFail" | "timeout";

export interface Purchase {
  id: string;
  scenario: ScenarioKey;
  vendor: string;
  item: string;
  itemLabel: string;
  amount: Milli;
  toolCall: { tool: string; arguments: Record<string, string> };
  rationale: string;
  injected: string | null;
  attempt: number;
  idempotencyKey: string;
  stage: Stage;
  decision: Decision | null;
  clause: Clause | null;
  policyVersion: number | null;
  policyHash: string | null;
  risk: number | null;
  expiresAt: number | null;
  approvedByHuman: boolean;
  qatomTx: string | null;
  latencyMs: number | null;
  delivery: "NONE" | "PENDING" | "VERIFIED" | "FAILED";
  recovery: "DELIVERY_FAILED" | "OUTCOME_UNKNOWN" | null;
  retriesSuppressed: number;
  revokedKey: string | null;
  walletBefore: Milli;
  createdAt: number;
}

export interface Claim {
  key: string;
  purchaseId: string;
  state: "CLAIMED" | "SETTLED" | "UNKNOWN";
}

export type EvtType =
  | "POLICY_SNAPSHOT"
  | "INTENT_PROPOSED"
  | "POLICY_EVALUATED"
  | "BUDGET_RESERVED"
  | "APPROVAL_REQUESTED"
  | "APPROVAL_GRANTED"
  | "APPROVAL_REJECTED"
  | "APPROVAL_EXPIRED"
  | "HOLD_RELEASED"
  | "DISPATCH_CLAIMED"
  | "PAYMENT_SETTLED"
  | "DUPLICATE_CALLBACK_IGNORED"
  | "DELIVERY_VERIFIED"
  | "DELIVERY_FAILED"
  | "DELIVERY_RECOVERED"
  | "OUTCOME_UNKNOWN"
  | "RETRY_SUPPRESSED"
  | "PURCHASE_BLOCKED"
  | "SESSION_KEY_REVOKED"
  | "SESSION_KEY_ISSUED"
  | "AGENT_FROZEN"
  | "AGENT_RESUMED";

export interface EvtBody {
  seq: number;
  ts: number;
  type: EvtType;
  actor: "agent" | "gateway" | "owner" | "approver" | "executor" | "seller";
  purchaseId: string | null;
  amount: Milli | null;
  clause: Clause | null;
  ref: string | null;
  detail: string;
  policyHash: string;
}
export interface Evt extends EvtBody {
  prev: string;
  hash: string;
}

export interface State {
  policy: Policy;
  policyHash: string;
  held: Milli;
  settled: Milli;
  wallet: Milli;
  purchases: Purchase[];
  events: Evt[];
  claims: Record<string, Claim>;
  session: { key: string; n: number; state: "active" | "revoked" };
  counter: number;
}

export type TxResult = { ok: true } | { ok: false; error: string };

export type Verification =
  | { status: "idle" }
  | { status: "running" }
  | { status: "intact"; count: number; head: string; engine: string }
  | { status: "broken"; at: number; count: number };

// Gateway wire contracts. Do not substitute simulated Stage or Clause values.
export type BackendPurchaseStage =
  | "PROPOSED" | "PENDING_APPROVAL" | "RESERVED" | "DISPATCHED"
  | "SETTLED" | "VERIFIED" | "BLOCKED" | "REJECTED" | "EXPIRED"
  | "RECOVERY_UNKNOWN" | "RECOVERY_UNDELIVERED";

export type BackendPolicyClause =
  | "WITHIN_AUTO_APPROVE_LIMIT" | "ABOVE_AUTO_APPROVE_LIMIT"
  | "VENDOR_NOT_WHITELISTED" | "VENDOR_ADDRESS_MISMATCH"
  | "VENDOR_UNLISTED_NEEDS_REVIEW" | "ABOVE_TRANSACTION_CEILING"
  | "ATTEMPT_CEILING_REACHED" | "INSUFFICIENT_ALLOCATION"
  | "AGENT_FROZEN" | "SESSION_KEY_INVALID"
  | "POLICY_EXPIRED" | "ARGUMENT_CONSTRAINT_VIOLATION";

export interface PurchaseRequestDTO {
  request_key: string;
  seller_domain: string;
  seller_address: string;
  item: string;
  amount: string; // Decimal USD-TDN, at most three fractional digits.
  tool_call?: Record<string, unknown>;
}

export interface LedgerEventDTO {
  seq: number;
  ts: number;
  type: string;
  actor: string;
  purchase_id: string | null;
  amount: Milli | null; // integer thousandths of USD-TDN
  clause: string | null;
  ref: string | null;
  detail: string;
  policy_hash: string;
  prev: string;
  hash: string;
}

// Controlled seller wire contracts. Prices remain decimal strings plus explicit milli.
export interface SellerCatalogItemDTO {
  item: string;
  label: string;
  amount: string;
  amount_milli: Milli;
  unit: "USD-TDN";
  synthetic: true;
  input_schema: Record<string, unknown>;
}

export interface SellerDeliveryDTO {
  schema: "mandate.seller-delivery.v1";
  item: string;
  request_id: string;
  rail_order_id: string;
  amount: string;
  amount_milli: Milli;
  created_at: string; // UTC ISO 8601 timestamp; freshness is a separate delivery check.
  synthetic: true;
  data: Record<string, unknown>;
}

export interface AgentDiscoveryDTO {
  mandate_id: string;
  rail: string;
  unit: "USD-TDN";
  sellers: Record<string, string>;
  items: SellerCatalogItemDTO[];
}

export type AgentAction =
  | { tool: "discover"; arguments?: Record<string, never> }
  | { tool: "propose"; arguments: PurchaseRequestDTO }
  | { tool: "status"; arguments: { purchase_id: string } };
