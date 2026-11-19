"use client";

import { signIn, signOut } from "next-auth/react";

export function GoogleSignIn() {
  return (
    <button type="button" className="btn-ghost" onClick={() => void signIn("google", { callbackUrl: "/console" })}>
      Sign in with Google
    </button>
  );
}

export function SignOutButton() {
  return (
    <button type="button" className="btn-ghost" onClick={() => void signOut({ callbackUrl: "/signin" })}>
      Sign out
    </button>
  );
}
