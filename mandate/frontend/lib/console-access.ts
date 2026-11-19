import { getServerSession } from "next-auth";
import { authOptions } from "./auth";

export type ConsoleStatus = "pending" | "approved" | "rejected";
export type ConsoleUser = {
  email: string;
  status: ConsoleStatus;
  created_at: number;
  updated_at: number;
};

function env(name: string): string {
  return process.env[name] ?? "";
}

export function ownerEmail(): string {
  return env("MANDATE_CONSOLE_OWNER_EMAIL").trim().toLowerCase();
}

export function isOwner(email: string): boolean {
  const owner = ownerEmail();
  return owner.length > 0 && email.trim().toLowerCase() === owner;
}

function gatewayBase(): string {
  const base = env("MANDATE_API_URL").replace(/\/$/, "");
  if (!base) throw new Error("MANDATE_API_URL is not set");
  return base;
}

async function gateway(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  headers.set("X-Owner-Token", env("MANDATE_OWNER_TOKEN"));
  if (init?.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  return fetch(`${gatewayBase()}${path}`, { ...init, headers, cache: "no-store" });
}

export async function ensureUser(email: string): Promise<ConsoleUser> {
  const created = await gateway("/api/v1/console-users", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
  if (!created.ok) throw new Error("console user lookup failed");
  let row = (await created.json()) as ConsoleUser;
  if (isOwner(email) && row.status !== "approved") {
    const approved = await gateway(`/api/v1/console-users/${encodeURIComponent(row.email)}/approve`, {
      method: "POST",
    });
    if (!approved.ok) throw new Error("could not approve the owner");
    row = (await approved.json()) as ConsoleUser;
  }
  return row;
}

export async function listUsers(): Promise<ConsoleUser[]> {
  const response = await gateway("/api/v1/console-users");
  if (!response.ok) throw new Error("console user list failed");
  const body = (await response.json()) as { users: ConsoleUser[] };
  return body.users;
}

export async function setStatus(email: string, status: "approved" | "rejected"): Promise<void> {
  const response = await gateway(`/api/v1/console-users/${encodeURIComponent(email)}/${status === "approved" ? "approve" : "reject"}`, {
    method: "POST",
  });
  if (!response.ok) throw new Error("console user update failed");
}

/** Null when the signed-in user may call the gateway. Otherwise a response to return. */
export async function requireApproved(): Promise<Response | null> {
  const session = await getServerSession(authOptions);
  const email = session?.user?.email;
  if (!email) return Response.json({ detail: "sign in required" }, { status: 401 });
  try {
    const row = await ensureUser(email);
    if (row.status !== "approved") return Response.json({ detail: "not approved" }, { status: 403 });
  } catch {
    return Response.json({ detail: "gateway unreachable" }, { status: 503 });
  }
  return null;
}
