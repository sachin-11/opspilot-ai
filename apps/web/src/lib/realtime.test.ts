import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { invalidateRealtimeEvent, reconnectDelay, type RealtimeEvent } from "./realtime";

const workspaceId = "11111111-1111-4111-8111-111111111111";
const incidentId = "22222222-2222-4222-8222-222222222222";
const event: RealtimeEvent = {
  event_id: "33333333-3333-4333-8333-333333333333",
  type: "incident.status_changed",
  workspace_id: workspaceId,
  incident_id: incidentId,
  occurred_at: "2026-10-30T10:00:00Z",
  data: {},
};

describe("realtime query routing", () => {
  it("invalidates only the active workspace and affected incident", async () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await invalidateRealtimeEvent(client, workspaceId, event);
    expect(invalidate).toHaveBeenCalledWith({queryKey: ["incidents", workspaceId]});
    expect(invalidate).toHaveBeenCalledWith({queryKey: ["incident", workspaceId, incidentId]});
    expect(invalidate).toHaveBeenCalledWith({queryKey: ["timeline", workspaceId, incidentId]});
    invalidate.mockClear();
    await invalidateRealtimeEvent(client, "44444444-4444-4444-8444-444444444444", event);
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("uses capped exponential reconnect backoff", () => {
    expect(reconnectDelay(0)).toBe(1000);
    expect(reconnectDelay(4)).toBe(16_000);
    expect(reconnectDelay(20)).toBe(30_000);
  });

  it("invalidates investigation list and detail for progress events", async () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await invalidateRealtimeEvent(client, workspaceId, {
      ...event,
      type: "investigation.step_completed",
      data: {investigation_id: "55555555-5555-4555-8555-555555555555"},
    });
    expect(invalidate).toHaveBeenCalledWith({queryKey: ["investigations", workspaceId]});
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["investigation", workspaceId, "55555555-5555-4555-8555-555555555555"],
    });
  });

  it("refreshes repository queries and stores indexing progress", async () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const repositoryId = "77777777-7777-4777-8777-777777777777";
    await invalidateRealtimeEvent(client, workspaceId, {...event, type: "repository.indexing_progress", incident_id: null, data: {repository_id: repositoryId, stage: "chunking", files_processed: 2, total_files: 4}});
    expect(invalidate).toHaveBeenCalledWith({queryKey: ["repositories", workspaceId]});
    expect(invalidate).toHaveBeenCalledWith({queryKey: ["repository", workspaceId, repositoryId]});
    expect(invalidate).toHaveBeenCalledWith({queryKey: ["github-installations", workspaceId]});
    expect(invalidate).toHaveBeenCalledWith({queryKey: ["service-mappings", workspaceId]});
    expect(invalidate).toHaveBeenCalledWith({queryKey: ["suspect-changes", workspaceId]});
    expect(client.getQueryData(["repository-progress", workspaceId, repositoryId])).toMatchObject({type: "repository.indexing_progress", stage: "chunking", files_processed: 2});
  });

  it("refreshes knowledge list and document detail for ingestion events", async () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await invalidateRealtimeEvent(client, workspaceId, {
      ...event,
      type: "knowledge.ingestion_completed",
      incident_id: null,
      data: {document_id: "66666666-6666-4666-8666-666666666666"},
    });
    expect(invalidate).toHaveBeenCalledWith({queryKey: ["knowledge", workspaceId]});
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["knowledge-document", workspaceId, "66666666-6666-4666-8666-666666666666"],
    });
  });
});
