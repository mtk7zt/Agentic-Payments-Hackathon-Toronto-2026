import { redirect } from "next/navigation";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { ensureUser } from "@/lib/console-access";
import ConsoleView from "./view";

export default async function ConsolePage() {
  if (process.env.NEXT_PUBLIC_MANDATE_PROXY !== "1") return <ConsoleView />;
  const session = await getServerSession(authOptions);
  const email = session?.user?.email;
  if (!email) redirect("/signin");
  let row;
  try {
    row = await ensureUser(email);
  } catch {
    redirect("/pending?state=offline");
  }
  if (row.status === "rejected") redirect("/pending?state=rejected");
  if (row.status !== "approved") redirect("/pending");
  return <ConsoleView />;
}
