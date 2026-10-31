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
export const investigationStatusSchema = z.enum(["queued", "running", "awaiting_review", "completed", "rejected", "failed", "cancelled"]);
export const investigationSummarySchema = z.object({
  id: z.string().uuid(), workspace_id: z.string().uuid(), incident_id: z.string().uuid(), retry_of_id: z.string().uuid().nullable(),
  status: investigationStatusSchema, trigger: z.enum(["manual", "incident_created", "incident_updated"]),
  prompt_version: z.string(), primary_provider: z.string(), primary_model: z.string(),
  fallback_provider: z.string().nullable(), fallback_model: z.string().nullable(), title: z.string().nullable(),
  executive_summary: z.string().nullable(), root_cause: z.string().nullable(), confidence: z.number().nullable(),
  total_input_tokens: z.number(), total_output_tokens: z.number(), estimated_cost_usd: z.number().nullable(),
  revision_count: z.number(), version: z.number(), started_at: z.string().nullable(), review_requested_at: z.string().nullable(),
  completed_at: z.string().nullable(), failed_at: z.string().nullable(), cancelled_at: z.string().nullable(),
  failure_code: z.string().nullable(), failure_message: z.string().nullable(), created_at: z.string(), updated_at: z.string(),
});
const planStepSchema = z.object({order: z.number(), action: z.string(), rationale: z.string(), evidence_ids: z.array(z.string().uuid())});
export const investigationReportSchema = z.object({
  title: z.string(), executive_summary: z.string(), root_cause: z.string(), confidence: z.number(), impact: z.string(),
  evidence_citations: z.array(z.string().uuid()), alternative_hypotheses: z.array(z.string()),
  immediate_containment_steps: z.array(planStepSchema), remediation_plan: z.array(planStepSchema),
  verification_plan: z.array(planStepSchema), unknowns_and_assumptions: z.array(z.string()), abstained: z.boolean(),
});
export const investigationDetailSchema = investigationSummarySchema.extend({
  steps: z.array(z.object({id: z.string().uuid(), node_name: z.string(), display_name: z.string(), status: z.string(), attempt: z.number(), summary: z.string().nullable(), structured_output: z.unknown().nullable(), error_code: z.string().nullable(), error_message: z.string().nullable(), input_tokens: z.number(), output_tokens: z.number(), estimated_cost_usd: z.number().nullable(), started_at: z.string(), completed_at: z.string().nullable()})),
  evidence: z.array(z.object({id: z.string().uuid(), evidence_type: z.string(), source_type: z.string(), source_id: z.string().nullable(), title: z.string(), excerpt: z.string(), metadata: z.record(z.string(), z.unknown()), content_hash: z.string(), collected_at: z.string()})),
  reviews: z.array(z.object({id: z.string().uuid(), action: z.string(), comment: z.string().nullable(), actor_id: z.string().nullable(), created_at: z.string()})),
  report: investigationReportSchema.nullable(),
});
export const knowledgeStatusSchema = z.enum(["draft", "processing", "ready", "failed", "archived"]);
export const documentTypeSchema = z.enum(["runbook", "postmortem", "architecture", "troubleshooting", "policy", "other"]);
export const knowledgeDocumentSchema = z.object({
  id: z.string().uuid(), workspace_id: z.string().uuid(), service_id: z.string().uuid().nullable(), title: z.string(), slug: z.string(),
  description: z.string().nullable(), document_type: documentTypeSchema, status: knowledgeStatusSchema,
  current_version_id: z.string().uuid().nullable(), tags: z.array(z.string()), created_at: z.string(), updated_at: z.string(), archived_at: z.string().nullable(),
  service_name: z.string().nullable(), current_version_number: z.number().nullable(), chunk_count: z.number(),
});
const knowledgeVersionSchema = z.object({id: z.string().uuid(), version_number: z.number(), original_filename: z.string().nullable(), mime_type: z.string(), content_hash: z.string(), byte_size: z.number(), page_count: z.number().nullable(), embedding_provider: z.string().nullable(), embedding_model: z.string().nullable(), embedding_dimensions: z.number().nullable(), chunk_count: z.number(), status: z.string(), failure_code: z.string().nullable(), failure_message: z.string().nullable(), created_at: z.string(), processed_at: z.string().nullable()});
export const knowledgeJobSchema = z.object({id: z.string().uuid(), workspace_id: z.string().uuid(), document_id: z.string().uuid(), document_version_id: z.string().uuid(), status: z.enum(["queued", "running", "completed", "failed", "cancelled"]), current_stage: z.string(), attempt: z.number(), chunks_processed: z.number(), total_chunks: z.number().nullable(), started_at: z.string().nullable(), completed_at: z.string().nullable(), failed_at: z.string().nullable(), failure_code: z.string().nullable(), failure_message: z.string().nullable(), created_at: z.string(), updated_at: z.string()});
export const knowledgeDetailSchema = knowledgeDocumentSchema.extend({versions: z.array(knowledgeVersionSchema), jobs: z.array(knowledgeJobSchema), chunks: z.array(z.object({id: z.string().uuid(), chunk_index: z.number(), content: z.string(), heading_path: z.array(z.string()), page_number: z.number().nullable(), token_count: z.number(), citation_label: z.string()}))});
const retrievalVariantSchema = z.enum(["lexical", "vector", "hybrid_rrf", "hybrid_rrf_rerank"]);
export const knowledgeSearchSchema = z.object({results: z.array(z.object({chunk_id: z.string().uuid(), document_id: z.string().uuid(), document_version_id: z.string().uuid(), document_title: z.string(), document_type: documentTypeSchema, service_id: z.string().uuid().nullable(), service_name: z.string().nullable(), heading_path: z.array(z.string()), page_number: z.number().nullable(), excerpt: z.string(), lexical_rank: z.number().nullable(), lexical_score: z.number().nullable(), vector_rank: z.number().nullable(), vector_score: z.number().nullable(), rrf_score: z.number().nullable(), rerank_score: z.number().nullable(), citation_label: z.string()})), diagnostics: z.object({variant: retrievalVariantSchema, candidate_count: z.number(), latency_ms: z.number(), embedding_latency_ms: z.number().nullable(), rerank_latency_ms: z.number().nullable(), rerank_fallback_used: z.boolean(), low_relevance: z.boolean()})});
const queuedKnowledgeSchema = z.object({document_id: z.string().uuid(), version_id: z.string().uuid(), job_id: z.string().uuid(), status: z.literal("queued")});

export const installationStatusSchema = z.enum(["active", "suspended", "removed"]);
export const repositoryStatusSchema = z.enum(["active", "syncing", "failed", "removed", "archived"]);
export const snapshotStatusSchema = z.enum(["queued", "indexing", "ready", "failed", "cancelled"]);
export const codeVariantSchema = z.enum(["lexical", "vector", "hybrid_rrf", "hybrid_rrf_rerank"]);
export const installationSchema = z.object({
  id: z.string().uuid(), workspace_id: z.string().uuid(), provider_installation_id: z.number(), account_login: z.string(), account_type: z.string(),
  status: installationStatusSchema, repository_selection: z.string(), permissions: z.record(z.string(), z.unknown()), rate_limit: z.record(z.string(), z.unknown()),
  installed_at: z.string(), suspended_at: z.string().nullable(), removed_at: z.string().nullable(), updated_at: z.string(),
  repository_count: z.number(), last_synced_at: z.string().nullable(),
});
export const installationListSchema = z.object({provider: z.string(), provider_configured: z.boolean(), configuration_hint: z.string().nullable(), installations: z.array(installationSchema)});
export const snapshotSchema = z.object({
  id: z.string().uuid(), commit_sha: z.string(), branch: z.string(), status: snapshotStatusSchema, file_count: z.number(), chunk_count: z.number(),
  stats: z.record(z.string(), z.unknown()), started_at: z.string().nullable(), completed_at: z.string().nullable(), failed_at: z.string().nullable(),
  failure_code: z.string().nullable(), failure_message: z.string().nullable(), created_at: z.string(),
});
export const mappedServiceSchema = z.object({mapping_id: z.string().uuid(), service_id: z.string().uuid(), service_name: z.string(), environment: environmentSchema, branch_pattern: z.string(), path_prefix: z.string().nullable()});
export const repositorySummarySchema = z.object({
  id: z.string().uuid(), workspace_id: z.string().uuid(), github_installation_id: z.string().uuid(), provider_repository_id: z.number(), owner: z.string(), name: z.string(),
  full_name: z.string(), default_branch: z.string(), is_private: z.boolean(), is_archived: z.boolean(), status: repositoryStatusSchema,
  failure_code: z.string().nullable(), failure_message: z.string().nullable(), last_synced_at: z.string().nullable(), updated_at: z.string(),
  active_snapshot: snapshotSchema.nullable().default(null), connected_services: z.array(mappedServiceSchema).default([]),
});
export const repositoryDetailSchema = repositorySummarySchema.extend({recent_snapshots: z.array(snapshotSchema).default([]), commit_count: z.number().default(0), deployment_count: z.number().default(0)});
export const queuedSchema = z.object({status: z.string(), detail: z.string().nullable().default(null), installation_id: z.string().uuid().nullable().default(null), repository_id: z.string().uuid().nullable().default(null), snapshot_id: z.string().uuid().nullable().default(null)});
export const mappingSchema = z.object({
  id: z.string().uuid(), workspace_id: z.string().uuid(), service_id: z.string().uuid(), repository_id: z.string().uuid(), repository_full_name: z.string(),
  environment: environmentSchema, branch_pattern: z.string(), path_prefix: z.string().nullable(), created_at: z.string(), updated_at: z.string(),
});
export const codeSearchResultSchema = z.object({
  chunk_id: z.string().uuid(), repository_id: z.string().uuid(), repository_full_name: z.string(), snapshot_id: z.string().uuid(), commit_sha: z.string(), branch: z.string(),
  path: z.string(), language: z.string().nullable(), symbol_name: z.string().nullable(), symbol_type: z.string().nullable(), start_line: z.number(), end_line: z.number(), excerpt: z.string(),
  lexical_rank: z.number().nullable().default(null), lexical_score: z.number().nullable().default(null), vector_rank: z.number().nullable().default(null), vector_score: z.number().nullable().default(null),
  rrf_score: z.number().nullable().default(null), rerank_score: z.number().nullable().default(null), citation_label: z.string(),
});
export const codeSearchSchema = z.object({
  results: z.array(codeSearchResultSchema),
  diagnostics: z.object({variant: codeVariantSchema, candidate_count: z.number(), latency_ms: z.number(), embedding_latency_ms: z.number().nullable(), rerank_latency_ms: z.number().nullable(), rerank_fallback_used: z.boolean(), low_relevance: z.boolean()}),
});
export const commitSchema = z.object({
  id: z.string().uuid(), repository_id: z.string().uuid(), repository_full_name: z.string(), sha: z.string(), author_name: z.string().nullable(), message: z.string(),
  committed_at: z.string(), changed_files: z.number(), additions: z.number(), deletions: z.number(), provider_url: z.string(),
  files: z.array(z.object({path: z.string(), previous_path: z.string().nullable(), change_type: z.string(), additions: z.number(), deletions: z.number()})).default([]),
});
export const deploymentSchema = z.object({
  id: z.string().uuid(), repository_id: z.string().uuid(), repository_full_name: z.string(), provider_deployment_id: z.number(), commit_sha: z.string(), environment: z.string(),
  status: z.string(), description: z.string().nullable(), deployed_at: z.string().nullable(), completed_at: z.string().nullable(),
});
export const suspectChangeSchema = z.object({
  kind: z.string(), repository_id: z.string().uuid(), repository_full_name: z.string(), commit_sha: z.string(), commit_id: z.string().uuid().nullable(), deployment_id: z.string().uuid().nullable(),
  title: z.string(), score: z.number(), confidence: z.string(), matched_signals: z.array(z.string()), contradicting_signals: z.array(z.string()), minutes_before_incident: z.number(),
  changed_files: z.array(z.string()), environment: z.string().nullable(), citation_label: z.string(),
});
export const suspectChangesSchema = z.object({
  incident_id: z.string().uuid(), opened_at: z.string(), service_id: z.string().uuid().nullable(), has_repository_mapping: z.boolean(),
  candidates: z.array(suspectChangeSchema), deployments: z.array(deploymentSchema), code_excerpts: z.array(codeSearchResultSchema), notice: z.string(),
});
export const repositoryProgressSchema = z.object({
  repository_id: z.string().uuid().optional(), installation_id: z.string().uuid().optional(), snapshot_id: z.string().uuid().optional(), status: z.string().optional(), stage: z.string().optional(),
  files_processed: z.number().optional(), total_files: z.number().optional(), chunks_processed: z.number().optional(), failure_code: z.string().optional(),
});

export type Workspace = z.infer<typeof workspaceSchema>;
export type InstallationList = z.infer<typeof installationListSchema>;
export type Installation = z.infer<typeof installationSchema>;
export type RepositorySummary = z.infer<typeof repositorySummarySchema>;
export type RepositoryDetail = z.infer<typeof repositoryDetailSchema>;
export type Snapshot = z.infer<typeof snapshotSchema>;
export type Mapping = z.infer<typeof mappingSchema>;
export type CodeSearch = z.infer<typeof codeSearchSchema>;
export type CodeSearchResult = z.infer<typeof codeSearchResultSchema>;
export type CodeVariant = z.infer<typeof codeVariantSchema>;
export type SuspectChanges = z.infer<typeof suspectChangesSchema>;
export type RepositoryProgress = z.infer<typeof repositoryProgressSchema>;
export type CodeSearchRequest = {query: string; repository_id?: string; service_id?: string; environment?: Environment; branch?: string; path_prefix?: string; language?: string; symbol_type?: string; variant: CodeVariant; candidate_count: number; top_k: number};
export type MappingCreate = {repository_id: string; environment: Environment; branch_pattern: string; path_prefix: string | null};
export type Service = z.infer<typeof serviceSchema>;
export type Incident = z.infer<typeof incidentSchema>;
export type Severity = z.infer<typeof severitySchema>;
export type IncidentStatus = z.infer<typeof statusSchema>;
export type Environment = z.infer<typeof environmentSchema>;
export type TimelineEvent = z.infer<typeof timelineSchema>;
export type InvestigationSummary = z.infer<typeof investigationSummarySchema>;
export type InvestigationDetail = z.infer<typeof investigationDetailSchema>;
export type ReviewAction = "approved" | "revision_requested" | "rejected";
export type KnowledgeDocument = z.infer<typeof knowledgeDocumentSchema>;
export type KnowledgeDetail = z.infer<typeof knowledgeDetailSchema>;
export type KnowledgeJob = z.infer<typeof knowledgeJobSchema>;
export type KnowledgeSearch = z.infer<typeof knowledgeSearchSchema>;
export type DocumentType = z.infer<typeof documentTypeSchema>;
export type RetrievalVariant = z.infer<typeof retrievalVariantSchema>;

export const sourceProviderSchema = z.enum(["generic_webhook", "aws_cloudwatch", "demo_fixture"]);
export const sourceStatusSchema = z.enum(["active", "disabled", "failing"]);
export const sourceSchema = z.object({
  id: z.string().uuid(), workspace_id: z.string().uuid(), service_id: z.string().uuid(), service_name: z.string().nullable(),
  public_id: z.string(), name: z.string(), provider: sourceProviderSchema, source_type: z.string(), status: sourceStatusSchema,
  environment: environmentSchema, region: z.string().nullable(), configuration: z.record(z.string(), z.unknown()),
  has_secret: z.boolean(), webhook_path: z.string().nullable(), last_event_at: z.string().nullable(),
  last_success_at: z.string().nullable(), last_failure_at: z.string().nullable(), created_at: z.string(), updated_at: z.string(),
});
export const sourceCreatedSchema = sourceSchema.extend({webhook_secret: z.string().nullable()});
export const secretRotatedSchema = z.object({source_id: z.string().uuid(), webhook_secret: z.string(), rotated_at: z.string()});
export const sourceTestSchema = z.object({
  ok: z.boolean(), status: z.enum(["ok", "throttled", "auth_failed", "unavailable", "misconfigured"]), message: z.string(), latency_ms: z.number(),
});
export const signalSummarySchema = z.object({
  signal_id: z.string().uuid(), source_id: z.string().uuid(), source_name: z.string().nullable(), provider: z.string(),
  service_id: z.string().uuid(), service_name: z.string().nullable(), signal_type: z.string().nullable(), severity: z.string().nullable(),
  title: z.string().nullable(), status: z.string(), received_at: z.string(), occurred_at: z.string().nullable(),
  processed_at: z.string().nullable(), incident_id: z.string().uuid().nullable(), ingest_result: z.string().nullable(),
  deduplication_key: z.string(), duplicate_of: z.string().uuid().nullable(), attempt_count: z.number(), failure_code: z.string().nullable(),
});
export const signalDetailSchema = signalSummarySchema.extend({
  provider_event_id: z.string().nullable(), payload_hash: z.string(), schema_version: z.string(),
  failure_message: z.string().nullable(), normalized: z.record(z.string(), z.unknown()).nullable(),
});
export const signalPageSchema = z.object({items: z.array(signalSummarySchema), next_cursor: z.string().nullable()});
export const sourceHealthSchema = z.object({
  id: z.string().uuid(), name: z.string(), provider: z.string(), status: z.string(),
  last_event_at: z.string().nullable(), last_success_at: z.string().nullable(), last_failure_at: z.string().nullable(),
});
export const pipelineStatusSchema = z.object({
  window_minutes: z.number(), generated_at: z.string(), accepted: z.number(), processed: z.number(), duplicates: z.number(),
  failures: z.number(), dead_letters_open: z.number(), events_per_second: z.number(), latency_p50_ms: z.number().nullable(),
  latency_p95_ms: z.number().nullable(), incidents_created: z.number(), events_correlated: z.number(),
  consumer_lag: z.number().nullable(), lag_available: z.boolean(), broker_healthy: z.boolean(), broker_backend: z.string(),
  backpressure: z.object({active: z.boolean(), reasons: z.array(z.string())}), sources: z.array(sourceHealthSchema),
});
export const deadLetterSchema = z.object({
  id: z.string().uuid(), workspace_id: z.string().uuid().nullable(), source_id: z.string().uuid().nullable(), source_name: z.string().nullable(),
  signal_id: z.string().uuid().nullable(), original_topic: z.string(), partition: z.number().nullable(), offset: z.number().nullable(),
  failure_code: z.string(), failure_class: z.enum(["retryable_exhausted", "non_retryable"]), failure_message: z.string(),
  attempt_count: z.number(), first_failed_at: z.string(), last_failed_at: z.string(), replayed_at: z.string().nullable(),
  resolved_at: z.string().nullable(), replay_count: z.number(), replayable: z.boolean(), payload_preview: z.record(z.string(), z.unknown()).nullable(),
});
export const deadLetterPageSchema = z.object({items: z.array(deadLetterSchema), next_cursor: z.string().nullable()});
export const replaySchema = z.object({
  status: z.enum(["replayed", "already_replayed"]), dead_letter_id: z.string().uuid(), replayed_at: z.string().nullable(), replay_count: z.number(),
});
export const demoGenerateResponseSchema = z.object({source_id: z.string().uuid(), accepted: z.number(), duplicates: z.number(), rejected: z.number()});
const logLineSchema = z.object({timestamp: z.string(), message: z.string(), severity: z.string().nullable().optional(), log_group: z.string().nullable().optional()});
const metricPointSchema = z.object({timestamp: z.string(), value: z.number()});
const metricComparisonSchema = z.object({
  metric_name: z.string(), statistic: z.string(), unit: z.string().nullable(), baseline_average: z.number().nullable(),
  incident_average: z.number().nullable(), change_percent: z.number().nullable(),
  baseline_points: z.array(metricPointSchema), incident_points: z.array(metricPointSchema),
});
const alarmContextSchema = z.object({
  alarm_name: z.string(), state: z.string(), metric_name: z.string().nullable(), threshold: z.number().nullable(),
  comparison: z.string().nullable(), period_seconds: z.number().nullable(),
  transitions: z.array(z.object({at: z.string(), from_state: z.string().nullable(), to_state: z.string(), reason: z.string().nullable()})),
});
export const incidentObservabilitySchema = z.object({
  incident_id: z.string().uuid(), has_source: z.boolean(), source_id: z.string().uuid().nullable(), source_name: z.string().nullable(),
  provider: z.string().nullable(), signals: z.array(signalSummarySchema), logs: z.array(logLineSchema),
  metric: metricComparisonSchema.nullable(), alarm: alarmContextSchema.nullable(), unavailable: z.string().nullable(), notice: z.string(),
});
export type Source = z.infer<typeof sourceSchema>;
export type SourceCreated = z.infer<typeof sourceCreatedSchema>;
export type SourceProvider = z.infer<typeof sourceProviderSchema>;
export type SourceTestResult = z.infer<typeof sourceTestSchema>;
export type SignalSummary = z.infer<typeof signalSummarySchema>;
export type SignalDetail = z.infer<typeof signalDetailSchema>;
export type PipelineStatus = z.infer<typeof pipelineStatusSchema>;
export type DeadLetter = z.infer<typeof deadLetterSchema>;
export type IncidentObservability = z.infer<typeof incidentObservabilitySchema>;
export type SourceCreate = {name: string; provider: SourceProvider; service_id: string; environment: Environment; region?: string | null; configuration: Record<string, unknown>};
export type SourceUpdate = {name?: string; status?: "active" | "disabled"; environment?: Environment; region?: string | null; configuration?: Record<string, unknown>};
export type DemoGenerateRequest = {source_id?: string; count: number; duplicate_percent: number; invalid_percent: number};

export const remediationStatusSchema = z.enum(["drafting", "policy_check", "policy_blocked", "sandbox_queued", "sandbox_running", "verification_failed", "awaiting_approval", "approved", "publishing", "pull_request_created", "rejected", "stale", "failed", "cancelled"]);
export const riskLevelSchema = z.enum(["low", "medium", "high", "prohibited"]);
export const policyDecisionSchema = z.enum(["allowed", "allowed_with_warnings", "blocked"]);
export const approvalActionSchema = z.enum(["approved", "rejected", "revision_requested"]);
export const remediationSummarySchema = z.object({
  id: z.string().uuid(), workspace_id: z.string().uuid(), incident_id: z.string().uuid(), investigation_id: z.string().uuid(),
  repository_id: z.string().uuid(), retry_of_id: z.string().uuid().nullable(), base_commit_sha: z.string(), status: remediationStatusSchema,
  title: z.string(), risk_level: riskLevelSchema, files_changed: z.number(), lines_added: z.number(), lines_deleted: z.number(),
  patch_hash: z.string().nullable(), policy_version: z.string().nullable(), policy_decision: policyDecisionSchema.nullable(),
  verification_state: z.string().nullable(), revision_count: z.number(), version: z.number(), created_at: z.string(), updated_at: z.string(),
  completed_at: z.string().nullable(), failed_at: z.string().nullable(), failure_code: z.string().nullable(), failure_message: z.string().nullable(),
  stale_reason: z.string().nullable(),
});
const policyIssueSchema = z.object({code: z.string(), message: z.string(), path: z.string().optional(), line: z.number().optional()});
export const verificationCheckSchema = z.object({
  id: z.string().uuid(), sandbox_run_id: z.string().uuid(), check_type: z.string(), command_id: z.string(), display_name: z.string(), status: z.string(),
  exit_code: z.number().nullable(), duration_ms: z.number(), stdout_excerpt: z.string().nullable(), stderr_excerpt: z.string().nullable(),
  output_truncated: z.boolean(), failing_tests: z.array(z.unknown()),
});
export const sandboxRunSchema = z.object({
  id: z.string().uuid(), revision_id: z.string().uuid(), run_kind: z.string(), status: z.string(), sandbox_provider: z.string(),
  base_commit_sha: z.string(), patch_hash: z.string(), baseline_run_id: z.string().uuid().nullable(), cpu_limit: z.number(),
  memory_limit_mb: z.number(), disk_limit_mb: z.number(), timeout_seconds: z.number(), network_enabled: z.boolean(),
  exit_code: z.number().nullable(), output_truncated: z.boolean(), result_hash: z.string().nullable(), cleanup_verified: z.boolean(),
  started_at: z.string().nullable(), completed_at: z.string().nullable(), failure_code: z.string().nullable(), failure_message: z.string().nullable(),
  created_at: z.string(), checks: z.array(verificationCheckSchema),
});
export const verificationComparisonSchema = z.object({
  state: z.enum(["passed", "partial", "failed", "not_run"]), required_executed: z.boolean(),
  new_failures: z.array(z.object({command_id: z.string(), tests: z.array(z.string())})),
  existing_failures: z.array(z.object({command_id: z.string(), tests: z.array(z.string())})),
  fixed: z.array(z.string()), skipped: z.array(z.object({command_id: z.string(), reason: z.string()})), notes: z.array(z.string()),
});
export const remediationVerificationSchema = z.object({
  remediation_id: z.string().uuid(), baseline: sandboxRunSchema.nullable(), patched: sandboxRunSchema.nullable(),
  comparison: verificationComparisonSchema.nullable(), verification_hash: z.string().nullable(),
});
export const revisionSchema = z.object({
  id: z.string().uuid(), revision_number: z.number(), base_commit_sha: z.string(), patch_text: z.string(), patch_hash: z.string(),
  change_summary: z.string(), files_changed: z.number(), lines_added: z.number(), lines_deleted: z.number(), created_at: z.string(),
});
export const approvalRecordSchema = z.object({
  id: z.string().uuid(), revision_id: z.string().uuid(), action: approvalActionSchema, actor_id: z.string().nullable(), comment: z.string(),
  approved_patch_hash: z.string(), approved_base_commit_sha: z.string(), approval_context_hash: z.string(),
  expires_at: z.string().nullable(), created_at: z.string(),
});
export const fixPullRequestSchema = z.object({
  id: z.string().uuid(), remediation_id: z.string().uuid(), revision_id: z.string().uuid(), repository_id: z.string().uuid(),
  base_commit_sha: z.string(), patch_hash: z.string(), branch_name: z.string(), commit_sha: z.string().nullable(),
  provider_pull_request_id: z.string().nullable(), pull_request_number: z.number().nullable(), pull_request_url: z.string().nullable(),
  status: z.enum(["creating", "created", "closed", "failed", "stale"]), failure_code: z.string().nullable(),
  failure_message: z.string().nullable(), created_at: z.string(), updated_at: z.string(),
});
export const remediationDetailSchema = remediationSummarySchema.extend({
  problem_statement: z.string(), proposed_solution: z.string(), affected_components: z.array(z.unknown()),
  generator_provider: z.string(), generator_model: z.string(), generator_prompt_version: z.string(),
  structured_output: z.record(z.string(), z.unknown()), total_input_tokens: z.number(), total_output_tokens: z.number(),
  estimated_cost_usd: z.number().nullable(), current_revision_id: z.string().uuid().nullable(),
  repository_full_name: z.string().nullable(), root_cause: z.string().nullable(),
  evidence: z.array(z.object({id: z.string().uuid(), title: z.string(), excerpt: z.string(), source_type: z.string()})),
  revisions: z.array(revisionSchema),
  policy_evaluations: z.array(z.object({
    id: z.string().uuid(), revision_id: z.string().uuid(), policy_version: z.string(), decision: policyDecisionSchema,
    risk_level: riskLevelSchema, violations: z.array(policyIssueSchema), warnings: z.array(policyIssueSchema), evaluated_at: z.string(),
  })),
  approvals: z.array(approvalRecordSchema),
  steps: z.array(z.object({id: z.string().uuid(), node_name: z.string(), display_name: z.string(), status: z.string(), attempt: z.number(), summary: z.string().nullable(), error_code: z.string().nullable(), started_at: z.string(), completed_at: z.string().nullable()})),
  pull_request: fixPullRequestSchema.nullable(),
  approval_binding: z.object({
    workspace_id: z.string().uuid(), repository_id: z.string().uuid(), base_commit_sha: z.string(), revision_id: z.string().uuid(),
    patch_hash: z.string(), policy_version: z.string(), verification_hash: z.string(), expires_in_seconds: z.number(),
  }).nullable(),
  write_enabled: z.boolean(),
  audit_events: z.array(z.object({id: z.string(), action: z.string(), result: z.string(), actor_type: z.string(), actor_id: z.string().nullable(), target_type: z.string(), correlation_id: z.string().nullable(), created_at: z.string()})),
});
const remediationQueuedSchema = z.object({remediation_id: z.string().uuid(), status: remediationStatusSchema});
const remediationActionSchema = z.object({remediation_id: z.string().uuid(), status: remediationStatusSchema, version: z.number()});
export type RemediationStatus = z.infer<typeof remediationStatusSchema>;
export type RemediationSummary = z.infer<typeof remediationSummarySchema>;
export type RemediationDetail = z.infer<typeof remediationDetailSchema>;
export type RemediationVerification = z.infer<typeof remediationVerificationSchema>;
export type SandboxRun = z.infer<typeof sandboxRunSchema>;
export type ApprovalAction = z.infer<typeof approvalActionSchema>;

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

async function requestEmpty(baseUrl: string, path: string, init: RequestInit): Promise<void> {
  const response = await fetch(`${baseUrl}${path}`, {...init, headers: {"Content-Type": "application/json", ...init.headers}});
  if (response.ok) return;
  const body: unknown = await response.json().catch(() => null);
  const parsed = z.object({error: z.object({message: z.string()})}).safeParse(body);
  throw new ApiError(response.status, parsed.success ? parsed.data.error.message : `Request failed (${response.status})`);
}

export function isProviderUnavailable(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 502 || error.status === 503);
}

async function multipartRequest<T>(baseUrl: string, path: string, schema: z.ZodType<T>, body: FormData): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {method: "POST", body});
  const value: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const parsed = z.object({error: z.object({message: z.string()})}).safeParse(value);
    throw new ApiError(response.status, parsed.success ? parsed.data.error.message : `Request failed (${response.status})`);
  }
  return schema.parse(value);
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
  remediations: (baseUrl: string, workspaceId: string, query: URLSearchParams) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/remediations?${query}`, z.array(remediationSummarySchema)),
  remediation: (baseUrl: string, workspaceId: string, remediationId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/remediations/${remediationId}`, remediationDetailSchema),
  remediationVerification: (baseUrl: string, workspaceId: string, remediationId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/remediations/${remediationId}/verification`, remediationVerificationSchema),
  startRemediation: (baseUrl: string, workspaceId: string, investigationId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/investigations/${investigationId}/remediations`, remediationQueuedSchema, {method: "POST"}),
  cancelRemediation: (baseUrl: string, workspaceId: string, remediationId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/remediations/${remediationId}/cancel`, remediationActionSchema, {method: "POST"}),
  retryRemediation: (baseUrl: string, workspaceId: string, remediationId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/remediations/${remediationId}/retry`, remediationQueuedSchema, {method: "POST"}),
  reviewRemediation: (baseUrl: string, workspaceId: string, remediationId: string, data: {action: ApprovalAction; comment: string; revision_id: string; version: number}) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/remediations/${remediationId}/review`, remediationActionSchema, {method: "POST", body: JSON.stringify(data)}),
  publishRemediation: (baseUrl: string, workspaceId: string, remediationId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/remediations/${remediationId}/publish`, remediationActionSchema, {method: "POST"}),
  investigations: (baseUrl: string, workspaceId: string, incidentId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/incidents/${incidentId}/investigations`, z.array(investigationSummarySchema)),
  investigation: (baseUrl: string, workspaceId: string, investigationId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/investigations/${investigationId}`, investigationDetailSchema),
  startInvestigation: (baseUrl: string, workspaceId: string, incidentId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/incidents/${incidentId}/investigations`, z.object({investigation_id: z.string().uuid(), status: z.literal("queued")}), {method: "POST", body: JSON.stringify({trigger: "manual"})}),
  reviewInvestigation: (baseUrl: string, workspaceId: string, investigationId: string, action: ReviewAction, comment: string | undefined, version: number) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/investigations/${investigationId}/review`, z.object({investigation_id: z.string().uuid(), status: investigationStatusSchema, version: z.number()}), {method: "POST", body: JSON.stringify({action, comment, version})}),
  cancelInvestigation: (baseUrl: string, workspaceId: string, investigationId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/investigations/${investigationId}/cancel`, z.object({investigation_id: z.string().uuid(), status: investigationStatusSchema, version: z.number()}), {method: "POST"}),
  retryInvestigation: (baseUrl: string, workspaceId: string, investigationId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/investigations/${investigationId}/retry`, z.object({investigation_id: z.string().uuid(), status: z.literal("queued")}), {method: "POST"}),
  knowledgeDocuments: (baseUrl: string, workspaceId: string, query = new URLSearchParams()) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/knowledge/documents?${query}`, z.array(knowledgeDocumentSchema)),
  knowledgeDocument: (baseUrl: string, workspaceId: string, documentId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/knowledge/documents/${documentId}`, knowledgeDetailSchema),
  createKnowledgeDocument: (baseUrl: string, workspaceId: string, form: FormData) => multipartRequest(baseUrl, `/api/v1/workspaces/${workspaceId}/knowledge/documents`, queuedKnowledgeSchema, form),
  addKnowledgeVersion: (baseUrl: string, workspaceId: string, documentId: string, form: FormData) => multipartRequest(baseUrl, `/api/v1/workspaces/${workspaceId}/knowledge/documents/${documentId}/versions`, queuedKnowledgeSchema, form),
  reindexKnowledgeDocument: (baseUrl: string, workspaceId: string, documentId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/knowledge/documents/${documentId}/reindex`, knowledgeJobSchema, {method: "POST"}),
  archiveKnowledgeDocument: (baseUrl: string, workspaceId: string, documentId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/knowledge/documents/${documentId}/archive`, z.object({document_id: z.string().uuid(), status: knowledgeStatusSchema}), {method: "POST"}),
  knowledgeSearch: (baseUrl: string, workspaceId: string, data: {query: string; service_id?: string; document_types: DocumentType[]; variant: RetrievalVariant; top_k: number; candidate_count: number}) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/knowledge/search`, knowledgeSearchSchema, {method: "POST", body: JSON.stringify(data)}),
  githubInstallations: (baseUrl: string, workspaceId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/integrations/github/installations`, installationListSchema),
  syncInstallation: (baseUrl: string, workspaceId: string, providerInstallationId: number) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/integrations/github/installations/sync`, queuedSchema, {method: "POST", body: JSON.stringify({provider_installation_id: providerInstallationId})}),
  repositories: (baseUrl: string, workspaceId: string, query = new URLSearchParams()) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/repositories?${query}`, z.array(repositorySummarySchema)),
  repository: (baseUrl: string, workspaceId: string, repositoryId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/repositories/${repositoryId}`, repositoryDetailSchema),
  syncRepository: (baseUrl: string, workspaceId: string, repositoryId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/repositories/${repositoryId}/sync`, queuedSchema, {method: "POST"}),
  reindexRepository: (baseUrl: string, workspaceId: string, repositoryId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/repositories/${repositoryId}/reindex`, queuedSchema, {method: "POST"}),
  repositoryCommits: (baseUrl: string, workspaceId: string, repositoryId: string, limit = 10) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/repositories/${repositoryId}/commits?limit=${limit}`, z.array(commitSchema)),
  repositoryDeployments: (baseUrl: string, workspaceId: string, repositoryId: string, limit = 10) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/repositories/${repositoryId}/deployments?limit=${limit}`, z.array(deploymentSchema)),
  serviceMappings: (baseUrl: string, workspaceId: string, serviceId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/services/${serviceId}/repositories`, z.array(mappingSchema)),
  createMapping: (baseUrl: string, workspaceId: string, serviceId: string, data: MappingCreate) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/services/${serviceId}/repositories`, mappingSchema, {method: "POST", body: JSON.stringify(data)}),
  deleteMapping: (baseUrl: string, workspaceId: string, serviceId: string, mappingId: string) => requestEmpty(baseUrl, `/api/v1/workspaces/${workspaceId}/services/${serviceId}/repositories/${mappingId}`, {method: "DELETE"}),
  codeSearch: (baseUrl: string, workspaceId: string, data: CodeSearchRequest) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/repositories/search`, codeSearchSchema, {method: "POST", body: JSON.stringify(data)}),
  suspectChanges: (baseUrl: string, workspaceId: string, incidentId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/incidents/${incidentId}/suspect-changes`, suspectChangesSchema),
  obsSources: (baseUrl: string, workspaceId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/observability/sources`, z.array(sourceSchema)),
  obsCreateSource: (baseUrl: string, workspaceId: string, data: SourceCreate) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/observability/sources`, sourceCreatedSchema, {method: "POST", body: JSON.stringify(data)}),
  obsUpdateSource: (baseUrl: string, workspaceId: string, sourceId: string, data: SourceUpdate) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/observability/sources/${sourceId}`, sourceSchema, {method: "PATCH", body: JSON.stringify(data)}),
  obsTestSource: (baseUrl: string, workspaceId: string, sourceId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/observability/sources/${sourceId}/test`, sourceTestSchema, {method: "POST"}),
  obsRotateSecret: (baseUrl: string, workspaceId: string, sourceId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/observability/sources/${sourceId}/rotate-secret`, secretRotatedSchema, {method: "POST"}),
  obsSignals: (baseUrl: string, workspaceId: string, query: URLSearchParams) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/observability/signals?${query}`, signalPageSchema),
  obsSignal: (baseUrl: string, workspaceId: string, signalId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/observability/signals/${signalId}`, signalDetailSchema),
  obsPipeline: (baseUrl: string, workspaceId: string, windowMinutes: number) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/observability/pipeline?window_minutes=${windowMinutes}`, pipelineStatusSchema),
  obsDeadLetters: (baseUrl: string, workspaceId: string, query: URLSearchParams) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/observability/dead-letters?${query}`, deadLetterPageSchema),
  obsReplay: (baseUrl: string, workspaceId: string, deadLetterId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/observability/dead-letters/${deadLetterId}/replay`, replaySchema, {method: "POST"}),
  obsDemoGenerate: (baseUrl: string, workspaceId: string, data: DemoGenerateRequest) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/observability/demo/generate`, demoGenerateResponseSchema, {method: "POST", body: JSON.stringify(data)}),
  incidentObservability: (baseUrl: string, workspaceId: string, incidentId: string) => request(baseUrl, `/api/v1/workspaces/${workspaceId}/incidents/${incidentId}/observability`, incidentObservabilitySchema),
};
