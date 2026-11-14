import type { Config } from "tailwindcss";

// Mandate tokens. Names match docs/ and the Figma variable map.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#020617", // slate-950
        surface: { DEFAULT: "#0f172a", raised: "#1e293b" }, // slate-900 / 800
        content: { DEFAULT: "#f1f5f9", secondary: "#94a3b8", inverse: "#020617" },
        status: {
          success: { DEFAULT: "#10b981", text: "#34d399" },
          warning: { DEFAULT: "#f59e0b", text: "#fbbf24" },
          danger: { DEFAULT: "#f43f5e", text: "#fb7185" },
          intent: { DEFAULT: "#0ea5e9", text: "#38bdf8" },
          on: "#020617",
        },
        focus: "#10b981",
      },
      fontFamily: {
        sans: ["var(--font-geist-sans)", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      ringColor: { focus: "#10b981" },
      ringOffsetColor: { canvas: "#020617" },
    },
  },
  plugins: [],
};

export default config;
