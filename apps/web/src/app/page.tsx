import { StatusDashboard } from "@/components/status-dashboard";
import { getApiBaseUrl } from "@/lib/config";

export default function HomePage() {
  const apiBaseUrl = getApiBaseUrl();

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-4xl flex-col gap-10 px-4 py-10 sm:px-8 sm:py-16">
      <header className="flex flex-col gap-3 border-b border-line pb-8">
        <p className="font-mono text-xs uppercase tracking-[0.2em] text-accent">Operations console</p>
        <h1 className="text-3xl font-semibold tracking-tight text-ink sm:text-4xl">OpsPilot AI</h1>
        <p className="max-w-xl text-base text-muted">
          Incident-response platform foundation. This page reports whether the core services are
          ready to work.
        </p>
      </header>
      <StatusDashboard apiBaseUrl={apiBaseUrl} />
    </main>
  );
}
