import { redirect } from "next/navigation";
import { getServerSession } from "next-auth";
import { SignOutButton } from "@/components/GoogleSignIn";
import { authOptions } from "@/lib/auth";
import { isOwner, listUsers, setStatus } from "@/lib/console-access";

async function decide(formData: FormData) {
  "use server";
  const session = await getServerSession(authOptions);
  const actor = session?.user?.email;
  if (!actor || !isOwner(actor)) redirect("/signin");
  const email = String(formData.get("email") ?? "");
  const status = String(formData.get("status") ?? "");
  if (status === "approved" || status === "rejected") await setStatus(email, status);
  redirect("/approvals");
}

export default async function ApprovalsPage() {
  const session = await getServerSession(authOptions);
  const email = session?.user?.email;
  if (!email) redirect("/signin");
  if (!isOwner(email)) redirect("/pending");
  const users = await listUsers();
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 px-6 py-10">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl">Approvals</h1>
        <SignOutButton />
      </div>
      <p className="text-sm text-slate-400">Signed-in Google accounts. Approve one to open the console.</p>
      <ul className="space-y-3">
        {users.length === 0 ? <li className="text-sm text-slate-500">No accounts yet.</li> : null}
        {users.map((user) => (
          <li key={user.email} className="plate flex flex-wrap items-center justify-between gap-3 rounded-md px-4 py-3">
            <div>
              <p className="text-sm">{user.email}</p>
              <p className="font-mono text-[12px] text-slate-500">{user.status}</p>
            </div>
            <form action={decide} className="flex gap-2">
              <input type="hidden" name="email" value={user.email} />
              <button className="btn-ghost" type="submit" name="status" value="approved">Approve</button>
              <button className="btn-ghost" type="submit" name="status" value="rejected">Reject</button>
            </form>
          </li>
        ))}
      </ul>
    </main>
  );
}
