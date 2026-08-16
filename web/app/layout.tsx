import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "local-platform · console",
  description:
    "Operator console for local-platform — grounded chat, tenant administration, and domain onboarding.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-cream-50 text-ink-800 antialiased">
        {children}
      </body>
    </html>
  );
}
