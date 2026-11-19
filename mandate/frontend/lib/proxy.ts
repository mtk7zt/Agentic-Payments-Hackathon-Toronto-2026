import { requireApproved } from "./console-access";

function env(name: string): string {
  return process.env[name] ?? "";
}

function gatewayBase(): string {
  const base = env("MANDATE_API_URL").replace(/\/$/, "");
  if (!base) throw new Error("MANDATE_API_URL is not set");
  return base;
}

/** Console approval is not a cockpit call. The approvals page talks to the gateway on the server. */
export function refusesConsoleUsers(path: string): boolean {
  const bare = path.split("?")[0].replace(/\/+$/, "").toLowerCase();
  return bare === "/api/v1/console-users" || bare.startsWith("/api/v1/console-users/");
}

/** Forward one cockpit call. The owner token stays on this server. */
export async function proxyGateway(request: Request, path: string): Promise<Response> {
  if (refusesConsoleUsers(path)) return Response.json({ detail: "not available" }, { status: 403 });
  const denied = await requireApproved();
  if (denied) return denied;
  const incoming = new URL(request.url);
  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  const accept = request.headers.get("accept");
  if (accept) headers.set("accept", accept);
  const sessionKey = request.headers.get("x-session-key");
  if (sessionKey) headers.set("X-Session-Key", sessionKey);
  headers.set("X-Owner-Token", env("MANDATE_OWNER_TOKEN"));
  const init: RequestInit = { method: request.method, headers, cache: "no-store" };
  if (request.method !== "GET" && request.method !== "HEAD") init.body = await request.arrayBuffer();
  let upstream: Response;
  try {
    upstream = await fetch(`${gatewayBase()}${path}${incoming.search}`, init);
  } catch {
    return Response.json({ detail: "gateway unreachable" }, { status: 503 });
  }
  const out = new Headers();
  const upstreamType = upstream.headers.get("content-type");
  if (upstreamType) out.set("content-type", upstreamType);
  out.set("cache-control", "no-store");
  const accel = upstream.headers.get("x-accel-buffering");
  if (accel) out.set("x-accel-buffering", accel);
  return new Response(upstream.body, { status: upstream.status, headers: out });
}
