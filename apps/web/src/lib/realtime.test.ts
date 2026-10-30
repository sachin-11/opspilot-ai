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
});
