import { proxyGateway } from "@/lib/proxy";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export function GET(request: Request) {
  return proxyGateway(request, "/api/events");
}
