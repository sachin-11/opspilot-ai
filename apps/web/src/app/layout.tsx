import type { Metadata } from "next";

import "./globals.css";
import { AppShell } from "@/components/app-shell";
import { OpsProviders } from "@/components/providers";
import { getApiBaseUrl } from "@/lib/config";

export const metadata: Metadata = {
  title: "OpsPilot AI",
  description: "OpsPilot AI incident operations console.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const baseUrl = getApiBaseUrl();
  return (
    <html lang="en">
      <body><OpsProviders baseUrl={baseUrl}><AppShell>{children}</AppShell></OpsProviders></body>
    </html>
  );
}
