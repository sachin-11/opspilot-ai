import type { QueryClient } from "@tanstack/react-query";
import type { z } from "zod";
import { realtimeSchema } from "./api";

export type RealtimeEvent = z.infer<typeof realtimeSchema>;

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
}
