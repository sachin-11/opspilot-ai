export type ServiceStatus = "checking" | "operational" | "down" | "unknown";

const PRESENTATION: Record<ServiceStatus, { label: string; glyph: string; tone: string }> = {
  checking: { label: "Checking", glyph: "…", tone: "border-line text-muted" },
  operational: { label: "Operational", glyph: "✓", tone: "border-ok/60 text-ok" },
  down: { label: "Down", glyph: "✕", tone: "border-bad/70 text-bad" },
  unknown: { label: "Unknown", glyph: "?", tone: "border-warn/60 text-warn" },
};

type StatusCardProps = {
  name: string;
  role: string;
  status: ServiceStatus;
};

export function StatusCard({ name, role, status }: StatusCardProps) {
  const { label, glyph, tone } = PRESENTATION[status];
  return (
    <li className="flex flex-col gap-4 rounded-md border border-line bg-panel p-5">
      <div>
        <h3 className="text-base font-semibold text-ink">{name}</h3>
        <p className="mt-1 text-sm text-muted">{role}</p>
      </div>
      <p
        className={`inline-flex w-fit items-center gap-2 rounded-sm border px-2.5 py-1 font-mono text-sm uppercase tracking-wider ${tone}`}
      >
        <span aria-hidden="true">{glyph}</span>
        <span>
          <span className="sr-only">{name} status: </span>
          {label}
        </span>
      </p>
    </li>
  );
}
