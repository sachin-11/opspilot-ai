"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { api, environmentSchema, type Environment, type SourceCreate, type SourceProvider } from "@/lib/api";
import { ObsFailure, SecretReveal, SigningHint, errorMessage } from "./observability-common";
import { useOps } from "./providers";

const providers: [SourceProvider, string][] = [["generic_webhook", "Generic webhook"], ["aws_cloudwatch", "AWS CloudWatch (read-only)"], ["demo_fixture", "Demo fixture"]];

function lines(value: FormDataEntryValue | null): string[] {
  return String(value ?? "").split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
}
function intField(form: FormData, name: string, fallback: number): number {
  const value = Number(form.get(name));
  return Number.isInteger(value) && value > 0 ? value : fallback;
}

export function SourceCreateForm() {
  const {baseUrl, workspace} = useOps(); const router = useRouter(); const client = useQueryClient();
  const [provider, setProvider] = useState<SourceProvider>("generic_webhook");
  const [error, setError] = useState(""); const [pending, setPending] = useState(false);
  const [created, setCreated] = useState<{name: string; webhookUrl: string | null; secret: string} | null>(null);
  const services = useQuery({queryKey: ["services", workspace?.id], enabled: Boolean(workspace), queryFn: () => api.services(baseUrl, workspace!.id)});

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError("");
    const form = new FormData(event.currentTarget);
    const name = String(form.get("name") ?? "").trim(); const serviceId = String(form.get("service_id") ?? "");
    const environment = environmentSchema.safeParse(form.get("environment"));
    if (!name) return setError("Enter a source name.");
    if (!serviceId) return setError("Select the service this source reports on.");
    if (!environment.success) return setError("Select an environment.");
    let configuration: Record<string, unknown> = {};
    if (provider === "aws_cloudwatch") {
      const roleArn = String(form.get("role_arn") ?? "").trim();
      if (!/^arn:aws[a-z-]*:iam::\d{12}:role\/.+/.test(roleArn)) return setError("Enter a valid IAM role ARN (arn:aws:iam::<account>:role/<name>).");
      configuration = {
        role_arn: roleArn, external_id_ref: String(form.get("external_id_ref") ?? "").trim(),
        allowed_log_groups: lines(form.get("allowed_log_groups")), allowed_namespaces: lines(form.get("allowed_namespaces")),
        max_window_minutes: intField(form, "max_window_minutes", 60), max_results: intField(form, "max_results", 100),
        timeout_seconds: intField(form, "timeout_seconds", 10), default_metric: String(form.get("default_metric") ?? "").trim(),
      };
    } else if (provider === "demo_fixture") {
      configuration = {fixture_mode: String(form.get("fixture_mode") ?? "ok")};
    }
    const body: SourceCreate = {name, provider, service_id: serviceId, environment: environment.data as Environment, region: String(form.get("region") ?? "").trim() || null, configuration};
    setPending(true);
    try {
      // Direct call: the one-time secret must not be stored in any query or mutation cache.
      const result = await api.obsCreateSource(baseUrl, workspace!.id, body);
      await client.invalidateQueries({queryKey: ["obs-sources", workspace?.id]});
      if (result.webhook_secret) setCreated({name: result.name, webhookUrl: result.webhook_path ? `${baseUrl}${result.webhook_path}` : null, secret: result.webhook_secret});
      else router.push("/observability/sources");
    } catch (e) { setError(errorMessage(e)); } finally { setPending(false); }
  }

  if (created) return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Observability</p><h2>Source created</h2><p className="muted">{created.name} is ready to receive signed webhooks.</p></div></header>
    <SecretReveal secret={created.secret} heading="Webhook secret" onDismiss={() => { setCreated(null); router.push("/observability/sources"); }}/>
    <section className="panel"><h3>Send your first event</h3><SigningHint webhookUrl={created.webhookUrl}/></section>
  </div>;

  return <div className="page-stack">
    <header className="page-header"><div><p className="eyebrow">Observability</p><h2>New source</h2></div></header>
    {services.isError && <ObsFailure error={services.error} what="services"/>}
    <form className="panel form" onSubmit={(event) => void submit(event)}>
      <fieldset className="mode-picker"><legend>Provider</legend>{providers.map(([value, label]) => <label key={value}><input type="radio" name="provider" value={value} checked={provider === value} onChange={() => setProvider(value)}/>{label}</label>)}</fieldset>
      <div className="form-grid">
        <label>Name<input name="name" required maxLength={120}/></label>
        <label>Service<select name="service_id" defaultValue=""><option value="">Select a service</option>{services.data?.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.environment}</option>)}</select></label>
        <label>Environment<select name="environment" defaultValue="production">{environmentSchema.options.map((item) => <option key={item}>{item}</option>)}</select></label>
        <label>Region (optional)<input name="region" maxLength={40} placeholder="e.g. us-east-1"/></label>
      </div>
      {provider === "generic_webhook" && <p className="notice">A signing secret is generated for this source and shown <strong>once</strong>. Anyone with the secret can submit signals for it, so store it in your secret manager.</p>}
      {provider === "aws_cloudwatch" && <fieldset className="form"><legend>CloudWatch read access</legend>
        <p className="notice" role="note"><strong>Never paste AWS access keys here.</strong> OpsPilot assumes a read-only IAM role you create. Reference the external id by its secret name, not its value. Grant only logs:FilterLogEvents, logs:GetLogEvents, cloudwatch:GetMetricData, and cloudwatch:DescribeAlarms.</p>
        <div className="form-grid">
          <label>IAM role ARN<input name="role_arn" placeholder="arn:aws:iam::123456789012:role/opspilot-readonly"/></label>
          <label>External id secret reference<input name="external_id_ref" placeholder="e.g. opspilot-cw-external-id"/></label>
          <label>Allowed log groups (one per line)<textarea name="allowed_log_groups" rows={3}/></label>
          <label>Allowed namespaces (one per line)<textarea name="allowed_namespaces" rows={3}/></label>
          <label>Max window (minutes)<input name="max_window_minutes" type="number" min={1} defaultValue={60}/></label>
          <label>Max results<input name="max_results" type="number" min={1} defaultValue={100}/></label>
          <label>Timeout (seconds)<input name="timeout_seconds" type="number" min={1} defaultValue={10}/></label>
          <label>Default metric<input name="default_metric" placeholder="e.g. CPUUtilization"/></label>
        </div></fieldset>}
      {provider === "demo_fixture" && <label>Fixture mode<select name="fixture_mode" defaultValue="ok">{["ok", "throttle", "timeout", "auth_failure", "error"].map((item) => <option key={item}>{item}</option>)}</select></label>}
      {error && <p role="alert" className="error">{error}</p>}
      <button className="button primary" disabled={pending}>{pending ? "Creating…" : "Create source"}</button>
    </form>
  </div>;
}
