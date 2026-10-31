import type { QueryClient, QueryKey } from "@tanstack/react-query";
import type { z } from "zod";
import { realtimeSchema, repositoryProgressSchema } from "./api";

export type RealtimeEvent = z.infer<typeof realtimeSchema>;

export function repositoryProgressKey(workspaceId: string, repositoryId: string): QueryKey {
  return ["repository-progress", workspaceId, repositoryId];
}

export function observabilityLiveKey(workspaceId: string): QueryKey {
  return ["obs-live", workspaceId];
}

export function reconnectDelay(attempt: number): number {
  return Math.min(30_000, 1000 * 2 ** Math.max(0, attempt));
}

export async function invalidateRealtimeEvent(
  queryClient: QueryClient,
  activeWorkspaceId: string,
  event: RealtimeEvent,
): Promise<void> {
  if (event.workspace_id !== activeWorkspaceId || event.type === "connection.established") return;
  await queryClient.invalidateQueries({queryKey: ["incidents", activeWorkspaceId]});
  if (event.incident_id) {
    await Promise.all([
      queryClient.invalidateQueries({queryKey: ["incident", activeWorkspaceId, event.incident_id]}),
      queryClient.invalidateQueries({queryKey: ["timeline", activeWorkspaceId, event.incident_id]}),
    ]);
  }
  if (event.type.startsWith("investigation.")) {
    await queryClient.invalidateQueries({queryKey: ["investigations", activeWorkspaceId]});
    const investigationId = event.data.investigation_id;
    if (typeof investigationId === "string") {
      await queryClient.invalidateQueries({queryKey: ["investigation", activeWorkspaceId, investigationId]});
    }
  }
  if (event.type.startsWith("remediation.")) {
    const keys: QueryKey[] = [["remediations", activeWorkspaceId]];
    const remediationId = event.data.remediation_id;
    if (typeof remediationId === "string") {
      keys.push(
        ["remediation", activeWorkspaceId, remediationId],
        ["remediation-verification", activeWorkspaceId, remediationId],
      );
    }
    await Promise.all(keys.map((queryKey) => queryClient.invalidateQueries({queryKey})));
  }
  if (event.type.startsWith("repository.")) {
    const progress = repositoryProgressSchema.safeParse(event.data);
    const repositoryId = progress.success ? progress.data.repository_id : undefined;
    if (progress.success && repositoryId && event.type.startsWith("repository.indexing_")) {
      queryClient.setQueryData(repositoryProgressKey(activeWorkspaceId, repositoryId), {type: event.type, ...progress.data});
    }
    const keys: QueryKey[] = [
      ["repositories", activeWorkspaceId], ["github-installations", activeWorkspaceId],
      ["service-mappings", activeWorkspaceId], ["suspect-changes", activeWorkspaceId],
    ];
    if (repositoryId) keys.push(["repository", activeWorkspaceId, repositoryId], ["repository-activity", activeWorkspaceId, repositoryId]);
    await Promise.all(keys.map((queryKey) => queryClient.invalidateQueries({queryKey})));
  }
  if (event.type.startsWith("observability.")) {
    queryClient.setQueryData(observabilityLiveKey(activeWorkspaceId), {type: event.type, occurred_at: event.occurred_at, ...event.data});
    const keys: QueryKey[] = [
      ["obs-pipeline", activeWorkspaceId], ["obs-signals", activeWorkspaceId], ["obs-sources", activeWorkspaceId],
      ["obs-dead-letters", activeWorkspaceId], ["obs-incident", activeWorkspaceId],
    ];
    const signalId = event.data.signal_id;
    if (typeof signalId === "string") keys.push(["obs-signal", activeWorkspaceId, signalId]);
    await Promise.all(keys.map((queryKey) => queryClient.invalidateQueries({queryKey})));
  }
  if (event.type.startsWith("knowledge.")) {
    await queryClient.invalidateQueries({queryKey: ["knowledge", activeWorkspaceId]});
    const documentId = event.data.document_id;
    if (typeof documentId === "string") {
      await queryClient.invalidateQueries({queryKey: ["knowledge-document", activeWorkspaceId, documentId]});
    }
  }
}
