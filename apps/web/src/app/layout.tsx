import type { Metadata } from "next";
import { cookies } from "next/headers";

import "./globals.css";
import { AppShell } from "@/components/app-shell";
import { OpsProviders } from "@/components/providers";
import { getAuthConfig } from "@/lib/auth/config";
import { openSession, SESSION_COOKIE } from "@/lib/auth/security";
import { getApiBaseUrl } from "@/lib/config";

// Every page depends on the request's session cookie, so nothing is prerendered at build time.
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: "OpsPilot AI",
  description: "OpsPilot AI incident operations console.",
};

export default async function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const wsBaseUrl = getApiBaseUrl();
  const config = getAuthConfig();
  const jar = await cookies();
  // Pages reached without a valid session are the public auth pages (the proxy redirects the rest).
  const session = await openSession(jar.get(SESSION_COOKIE)?.value, config.sessionSecret);
  return (
    <html lang="en">
      <body>
        {session
          ? <OpsProviders baseUrl="/api/proxy" wsBaseUrl={wsBaseUrl}><AppShell>{children}</AppShell></OpsProviders>
          : <main className="auth-page">{children}</main>}
      </body>
    </html>
  );
}
