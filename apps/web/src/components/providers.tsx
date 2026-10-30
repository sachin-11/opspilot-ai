"use client";

import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useRef, useState } from "react";
import { api, realtimeSchema, type Workspace } from "@/lib/api";
import { invalidateRealtimeEvent, reconnectDelay } from "@/lib/realtime";

type Connection = "connecting" | "live" | "degraded";
type OpsContextValue = {baseUrl: string; workspace: Workspace | null; workspaces: Workspace[]; connection: Connection};
const OpsContext = createContext<OpsContextValue | null>(null);

export function useOps() {
  const value = useContext(OpsContext);
  if (!value) throw new Error("useOps must be used inside OpsProviders");
  return value;
}

function WorkspaceProvider({baseUrl, children}: {baseUrl: string; children: React.ReactNode}) {
  const queryClient = useQueryClient();
  const workspacesQuery = useQuery({queryKey: ["workspaces"], queryFn: () => api.workspaces(baseUrl)});
  const workspaces = useMemo(() => workspacesQuery.data ?? [], [workspacesQuery.data]);
  const workspace = workspaces[0] ?? null;
  const [connection, setConnection] = useState<Connection>("connecting");
  const retry = useRef(0);

  useEffect(() => {
    if (!workspace) return;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    const connect = () => {
      setConnection("connecting");
      const wsBase = baseUrl.replace(/^http/, "ws");
      socket = new WebSocket(`${wsBase}/api/v1/ws/workspaces/${workspace.id}/incidents`);
      socket.onopen = () => { retry.current = 0; setConnection("live"); };
      socket.onmessage = (event) => {
        if (event.data === "pong") return;
        const parsed = realtimeSchema.safeParse(JSON.parse(String(event.data)));
        if (!parsed.success || parsed.data.workspace_id !== workspace.id || parsed.data.type === "connection.established") return;
        void invalidateRealtimeEvent(queryClient, workspace.id, parsed.data);
      };
      socket.onclose = () => {
        if (stopped) return;
        setConnection("degraded");
        const delay = reconnectDelay(retry.current++);
        timer = setTimeout(connect, delay);
      };
      socket.onerror = () => socket?.close();
    };
    connect();
    const heartbeat = setInterval(() => { if (socket?.readyState === WebSocket.OPEN) socket.send("ping"); }, 20_000);
    return () => { stopped = true; clearInterval(heartbeat); if (timer) clearTimeout(timer); socket?.close(); };
  }, [baseUrl, queryClient, workspace]);

  const value = useMemo(() => ({baseUrl, workspace, workspaces, connection}), [baseUrl, workspace, workspaces, connection]);
  if (workspacesQuery.isLoading) return <main className="center-state">Loading workspace…</main>;
  if (workspacesQuery.isError) return <main className="center-state error">Could not load workspaces. Check the API.</main>;
  return <OpsContext.Provider value={value}>{children}</OpsContext.Provider>;
}

export function OpsProviders({baseUrl, children}: {baseUrl: string; children: React.ReactNode}) {
  const [client] = useState(() => new QueryClient({defaultOptions: {queries: {staleTime: 5_000, retry: 1}}}));
  return <QueryClientProvider client={client}><WorkspaceProvider baseUrl={baseUrl}>{children}</WorkspaceProvider></QueryClientProvider>;
}
