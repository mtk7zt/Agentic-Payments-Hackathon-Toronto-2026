import type { Config } from "tailwindcss";

// Mandate tokens. Names match docs/ and the Figma variable map.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "var(--mandate-canvas)",
        surface: { DEFAULT: "var(--mandate-surface)", raised: "var(--mandate-raised)" },
        content: { DEFAULT: "var(--mandate-content)", secondary: "var(--mandate-secondary)", inverse: "#020617" },
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
      ringOffsetColor: { canvas: "var(--mandate-canvas)" },
    },
  },
  plugins: [],
};

export default config;
