import { redirect } from "next/navigation";
import { getServerSession } from "next-auth";
import { GoogleSignIn } from "@/components/GoogleSignIn";
import { authOptions } from "@/lib/auth";

export default async function SignInPage() {
  const session = await getServerSession(authOptions);
  if (session?.user?.email) redirect("/console");
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 px-6">
      <h1 className="text-2xl">Sign in</h1>
      <p className="text-sm text-slate-400">Use the Google account that should open this console.</p>
      <GoogleSignIn />
      <p className="text-sm text-slate-500">The owner reviews new accounts at /approvals.</p>
    </main>
  );
}
