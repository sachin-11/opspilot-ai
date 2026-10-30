import { z } from "zod";

export const severitySchema = z.enum(["sev1", "sev2", "sev3", "sev4"]);
export const statusSchema = z.enum(["open", "investigating", "mitigated", "resolved"]);
export const environmentSchema = z.enum(["development", "staging", "production"]);

export const workspaceSchema = z.object({
  id: z.string().uuid(), name: z.string(), slug: z.string(),
  created_at: z.string(), updated_at: z.string(),
});
export const serviceSchema = z.object({
  id: z.string().uuid(), workspace_id: z.string().uuid(), name: z.string(), slug: z.string(),
  description: z.string().nullable(), environment: environmentSchema,
  created_at: z.string(), updated_at: z.string(),
});
export const incidentSchema = z.object({
  id: z.string().uuid(), workspace_id: z.string().uuid(), service_id: z.string().uuid().nullable(),
  service_name: z.string().nullable(), service_environment: environmentSchema.nullable(),
  title: z.string(), description: z.string().nullable(), severity: severitySchema,
  status: statusSchema, source: z.enum(["manual", "event_ingestion"]), fingerprint: z.string().nullable(),
  version: z.number().int(), opened_at: z.string(), acknowledged_at: z.string().nullable(),
  mitigated_at: z.string().nullable(), resolved_at: z.string().nullable(),
  created_at: z.string(), updated_at: z.string(),
});
export const incidentListSchema = z.object({items: z.array(incidentSchema), next_cursor: z.string().nullable()});
export const timelineSchema = z.object({
  id: z.string().uuid(), workspace_id: z.string().uuid(), incident_id: z.string().uuid(),
  event_type: z.string(), actor_type: z.string(), actor_id: z.string().nullable(),
  summary: z.string(), metadata: z.record(z.string(), z.unknown()),
  occurred_at: z.string(), created_at: z.string(),
});
export const realtimeSchema = z.object({
  event_id: z.string().uuid(), type: z.string(), workspace_id: z.string().uuid(),
  incident_id: z.string().uuid().nullable(), occurred_at: z.string(),
  data: z.record(z.string(), z.unknown()),
});

export type Workspace = z.infer<typeof workspaceSchema>;
export type Service = z.infer<typeof serviceSchema>;
export type Incident = z.infer<typeof incidentSchema>;
export type Severity = z.infer<typeof severitySchema>;
export type IncidentStatus = z.infer<typeof statusSchema>;
export type Environment = z.infer<typeof environmentSchema>;
export type TimelineEvent = z.infer<typeof timelineSchema>;

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function request<T>(baseUrl: string, path: string, schema: z.ZodType<T>, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    ...init, headers: {"Content-Type": "application/json", ...init?.headers},
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const parsed = z.object({error: z.object({message: z.string()})}).safeParse(body);
    throw new ApiError(response.status, parsed.success ? parsed.data.error.message : `Request failed (${response.status})`);
  }
  return schema.parse(body);
}

export const api = {
  workspaces: (baseUrl: string) => request(baseUrl, "/api/v1/workspaces", z.array(workspaceSchema)),
  createWorkspace: (baseUrl: string, data: {name: string}) => request(baseUrl, "/api/v1/workspaces", workspaceSchema, {method: "POST", body: JSON.stringify(data)}),
  services: (baseUrl: string, workspaceId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/services`, z.array(serviceSchema)),
  createService: (baseUrl: string, workspaceId: string, data: {name: string; description?: string; environment: Environment}) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/services`, serviceSchema, {method: "POST", body: JSON.stringify(data)}),
  incidents: (baseUrl: string, workspaceId: string, query: URLSearchParams) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/incidents?${query}`, incidentListSchema),
  incident: (baseUrl: string, workspaceId: string, incidentId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/incidents/${incidentId}`, incidentSchema),
  timeline: (baseUrl: string, workspaceId: string, incidentId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/incidents/${incidentId}/timeline`, z.array(timelineSchema)),
  createIncident: (baseUrl: string, workspaceId: string, data: {title: string; description?: string; service_id?: string; severity: Severity}) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/incidents`, incidentSchema, {method: "POST", body: JSON.stringify(data)}),
  status: (baseUrl: string, workspaceId: string, incidentId: string, status: IncidentStatus, version: number) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/incidents/${incidentId}/status`, incidentSchema, {method: "PATCH", body: JSON.stringify({status, version})}),
  severity: (baseUrl: string, workspaceId: string, incidentId: string, severity: Severity, version: number) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/incidents/${incidentId}/severity`, incidentSchema, {method: "PATCH", body: JSON.stringify({severity, version})}),
  note: (baseUrl: string, workspaceId: string, incidentId: string, summary: string, version: number) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/incidents/${incidentId}/notes`, timelineSchema, {method: "POST", body: JSON.stringify({summary, version})}),
};
