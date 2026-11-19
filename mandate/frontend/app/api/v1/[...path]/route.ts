import { proxyGateway } from "@/lib/proxy";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

async function handle(request: Request, context: { params: { path: string[] } }) {
  const path = "/api/v1/" + context.params.path.map((part) => encodeURIComponent(part)).join("/");
  return proxyGateway(request, path);
}

export const GET = handle;
export const POST = handle;
export const PATCH = handle;
export const PUT = handle;
export const DELETE = handle;
