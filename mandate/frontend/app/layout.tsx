import type { Metadata } from "next";
import { GeistSans } from "geist/font/sans";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "Mandate · Procurement firewall for AI agents",
  description: "A deterministic policy gateway between autonomous LLM tool calls and live payments.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={GeistSans.variable}>
      <body>{children}</body>
    </html>
  );
}
