import type { NextAuthOptions } from "next-auth";
import GoogleProvider from "next-auth/providers/google";

function env(name: string): string {
  return process.env[name] ?? "";
}

export const authOptions: NextAuthOptions = {
  providers: [
    GoogleProvider({
      clientId: env("GOOGLE_CLIENT_ID") || "missing",
      clientSecret: env("GOOGLE_CLIENT_SECRET") || "missing",
    }),
  ],
  session: { strategy: "jwt" },
  pages: { signIn: "/signin" },
  callbacks: {
    async signIn({ account, profile }) {
      if (account?.provider !== "google") return false;
      const google = profile as { email_verified?: boolean } | undefined;
      return google?.email_verified === true;
    },
  },
};
