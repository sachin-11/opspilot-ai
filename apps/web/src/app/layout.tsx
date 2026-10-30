import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "OpsPilot AI",
  description: "OpsPilot AI operations console: service readiness.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
