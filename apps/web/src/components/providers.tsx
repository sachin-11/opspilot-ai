"use client";

import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { api, realtimeSchema, setCsrfToken, type MembershipSummary, type Me, type Role, type Workspace } from "@/lib/api";
import { invalidateRealtimeEvent, reconnectDelay } from "@/lib/realtime";

type Connection = "connecting" | "live" | "degraded";
type OpsContextValue = {
  baseUrl: string;
  workspace: Workspace | null;
  workspaces: Workspace[];
  connection: Connection;
  user: Me | null;
  role: Role | null;
  /** Server-resolved permissions for the active workspace. UX only: the API enforces them. */
  permissions: string[];
  selectWorkspace: (workspaceId: string) => void;
  refreshMemberships: () => Promise<void>;
};
const OpsContext = createContext<OpsContextValue | null>(null);
const STORAGE_KEY = "opspilot.workspace";

export function useOps() {
  const value = useContext(OpsContext);
  if (!value) throw new Error("useOps must be used inside OpsProviders");
  return value;
}

function Notice({title, children}: {title: string; children?: React.ReactNode}) {
  return <main className="center-state" role="alert"><h2>{title}</h2>{children}</main>;
}

function WorkspaceProvider({baseUrl, wsBaseUrl, children}: {baseUrl: string; wsBaseUrl: string; children: React.ReactNode}) {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string | null>(() => typeof window === "undefined" ? null : window.localStorage.getItem(STORAGE_KEY));
  const session = useQuery({queryKey: ["session"], retry: false, queryFn: async () => {
    const response = await fetch("/api/auth/session", {credentials: "same-origin"});
    if (!response.ok) throw new Error("session");
    const body = (await response.json()) as {csrfToken: string};
    setCsrfToken(body.csrfToken); // before any dependent request is issued
    return body;
  }});
  const csrfReady = session.isSuccess;
  useEffect(() => {
    if (session.isError && typeof window !== "undefined") window.location.href = `/session-expired?returnTo=${encodeURIComponent(window.location.pathname)}`;
  }, [session.isError]);
  const me = useQuery({queryKey: ["me"], enabled: csrfReady, queryFn: () => api.me(baseUrl)});
  const memberships = useQuery({queryKey: ["memberships"], enabled: csrfReady, queryFn: () => api.memberships(baseUrl)});
  const list: MembershipSummary[] = useMemo(() => memberships.data ?? [], [memberships.data]);
  const active = list.find((item) => item.workspace.id === selected) ?? list[0] ?? null;
  const workspace = active?.workspace ?? null;
  const workspaces = useMemo(() => list.map((item) => item.workspace), [list]);
  const [connection, setConnection] = useState<Connection>("connecting");
  const retry = useRef(0);
  const refreshMemberships = useCallback(async () => {
    await Promise.all([queryClient.invalidateQueries({queryKey: ["memberships"]}), queryClient.invalidateQueries({queryKey: ["me"]})]);
  }, [queryClient]);
  const selectWorkspace = useCallback((workspaceId: string) => {
    window.localStorage.setItem(STORAGE_KEY, workspaceId);
    setSelected(workspaceId);
    void queryClient.invalidateQueries();
  }, [queryClient]);

  const workspaceId = workspace?.id;
  useEffect(() => {
    if (!workspaceId || !csrfReady) return;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    const connect = async () => {
      setConnection("connecting");
      try {
        // A fresh single-use ticket for every (re)connection; no bearer token ever reaches the socket.
        const {ticket} = await api.wsTicket(baseUrl, workspaceId);
        if (stopped) return;
        socket = new WebSocket(`${wsBaseUrl.replace(/^http/, "ws")}/api/v1/ws/workspaces/${workspaceId}/incidents?ticket=${encodeURIComponent(ticket)}`);
      } catch {
        if (!stopped) { setConnection("degraded"); timer = setTimeout(() => void connect(), reconnectDelay(retry.current++)); }
        return;
      }
      socket.onopen = () => { retry.current = 0; setConnection("live"); };
      socket.onmessage = (event) => {
        if (event.data === "pong") return;
        const parsed = realtimeSchema.safeParse(JSON.parse(String(event.data)));
        if (!parsed.success || parsed.data.workspace_id !== workspaceId || parsed.data.type === "connection.established") return;
        void invalidateRealtimeEvent(queryClient, workspaceId, parsed.data);
      };
      socket.onclose = (event) => {
        if (stopped) return;
        setConnection("degraded");
        // 4403: membership revoked while connected. Reload memberships so access drops immediately.
        if (event.code === 4403) void refreshMemberships();
        timer = setTimeout(() => void connect(), reconnectDelay(retry.current++));
      };
      socket.onerror = () => socket?.close();
    };
    void connect();
    const heartbeat = setInterval(() => { if (socket?.readyState === WebSocket.OPEN) socket.send("ping"); }, 20_000);
    return () => { stopped = true; clearInterval(heartbeat); if (timer) clearTimeout(timer); socket?.close(); };
  }, [baseUrl, wsBaseUrl, queryClient, workspaceId, csrfReady, refreshMemberships]);

  const value = useMemo<OpsContextValue>(() => ({
    baseUrl, workspace, workspaces, connection, user: me.data ?? null, role: active?.role ?? null,
    permissions: active?.permissions ?? [], selectWorkspace, refreshMemberships,
  }), [baseUrl, workspace, workspaces, connection, me.data, active, selectWorkspace, refreshMemberships]);

  if (session.isLoading || memberships.isLoading || me.isLoading) return <main className="center-state" role="status">Loading workspace…</main>;
  if (memberships.isError) {
    const status = (memberships.error as {status?: number}).status;
    if (status === 401) return <Notice title="Session expired"><p>Please <a href="/login">sign in again</a>.</p></Notice>;
    return <Notice title="Could not load workspaces">Check the API.</Notice>;
  }
  return <OpsContext.Provider value={value}>{children}</OpsContext.Provider>;
}

export function OpsProviders({baseUrl, wsBaseUrl, children}: {baseUrl: string; wsBaseUrl: string; children: React.ReactNode}) {
  const [client] = useState(() => new QueryClient({defaultOptions: {queries: {staleTime: 5_000, retry: 1}}}));
  return <QueryClientProvider client={client}><WorkspaceProvider baseUrl={baseUrl} wsBaseUrl={wsBaseUrl}>{children}</WorkspaceProvider></QueryClientProvider>;
}
