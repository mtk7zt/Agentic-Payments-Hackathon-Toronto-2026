import { redirect } from "next/navigation";
import { getServerSession } from "next-auth";
import { SignOutButton } from "@/components/GoogleSignIn";
import { authOptions } from "@/lib/auth";
import { ensureUser } from "@/lib/console-access";

export default async function PendingPage({ searchParams }: { searchParams?: { state?: string } }) {
  const session = await getServerSession(authOptions);
  const email = session?.user?.email;
  if (!email) redirect("/signin");
  if (searchParams?.state !== "offline") {
    let row;
    try {
      row = await ensureUser(email);
    } catch {
      row = undefined;
    }
    if (row?.status === "approved") redirect("/console");
  }
  const rejected = searchParams?.state === "rejected";
  const offline = searchParams?.state === "offline";
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 px-6">
      <h1 className="text-2xl">Waiting for approval</h1>
      <p className="text-sm text-slate-400">
        {offline
          ? "The gateway could not be reached. Try again in a moment."
          : rejected
            ? `${email} was not approved.`
            : `${email} is waiting for the owner to approve this account.`}
      </p>
      <SignOutButton />
    </main>
  );
}
