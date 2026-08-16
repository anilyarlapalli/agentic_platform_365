import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Warm-cream base, carried over from the Azure console so the two
        // surfaces read as the same product family.
        cream: {
          50: "#fbfaf7",
          100: "#f7f5ee",
          200: "#ece7d8",
          300: "#ddd6c2",
        },
        ink: {
          900: "#1f1e1c",
          800: "#2d2c29",
          700: "#3d3b37",
          600: "#52504a",
          500: "#74716a",
          400: "#9a978f",
          300: "#bfbcb3",
        },
        copper: {
          400: "#e07a55",
          500: "#c25b3f",
          600: "#a64931",
          700: "#7f3724",
        },
        // Retrieval-signal colours. Each of the three signals that
        // `/api/query` attributes a source to gets a fixed hue, so a chunk's
        // provenance is readable at a glance and stays consistent between the
        // source list and the retrieval breakdown.
        signal: {
          dense: "#3f6fc2",
          lexical: "#3f9f7a",
          graph: "#8b5cf6",
        },
        // Status colours for platform state — budget caps, edgeless graphs,
        // failed runs. Kept distinct from `copper` so a brand accent is never
        // mistaken for a warning.
        warn: "#b45309",
        danger: "#b91c1c",
        ok: "#15803d",
      },
      fontFamily: {
        sans: [
          "ui-sans-serif",
          "-apple-system",
          "BlinkMacSystemFont",
          "Inter",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        serif: ["ui-serif", "Georgia", "Cambria", "Times New Roman", "serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
      boxShadow: {
        soft: "0 2px 24px -8px rgba(31, 30, 28, 0.12)",
        ring: "0 0 0 1px rgba(31, 30, 28, 0.06)",
      },
      borderRadius: {
        bubble: "22px",
      },
    },
  },
  plugins: [],
};

export default config;
