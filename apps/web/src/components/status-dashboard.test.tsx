import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StatusDashboard } from "./status-dashboard";

const BASE = "http://api.test";

function mockFetchOnce(status: number, body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("StatusDashboard", () => {
  it("shows a loading state before the first response", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<StatusDashboard apiBaseUrl={BASE} />);
    expect(screen.getByRole("status")).toHaveTextContent("Checking services");
  });

  it("shows ready when every dependency is up", async () => {
    mockFetchOnce(200, { status: "ready", dependencies: { postgres: "up", redis: "up" } });
    render(<StatusDashboard apiBaseUrl={BASE} />);

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("All systems ready"));
    expect(screen.getAllByText("Operational")).toHaveLength(3);
    expect(fetch).toHaveBeenCalledWith(`${BASE}/health/ready`, expect.objectContaining({ cache: "no-store" }));
  });

  it("shows degraded with the failing dependency marked down", async () => {
    mockFetchOnce(503, { status: "degraded", dependencies: { postgres: "up", redis: "down" } });
    render(<StatusDashboard apiBaseUrl={BASE} />);

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Degraded"));
    expect(screen.getByText("Down")).toBeInTheDocument();
    expect(screen.getAllByText("Operational")).toHaveLength(2);
  });

  it("shows backend unreachable when the request fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    render(<StatusDashboard apiBaseUrl={BASE} />);

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Backend unreachable"));
    expect(screen.queryByText("Operational")).not.toBeInTheDocument();
    expect(screen.getAllByText("Unknown")).toHaveLength(2);
  });

  it("treats a malformed 200 response as unreachable, not ready", async () => {
    mockFetchOnce(200, { hello: "world" });
    render(<StatusDashboard apiBaseUrl={BASE} />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Backend unreachable"));
  });

  it("refreshes on demand", async () => {
    mockFetchOnce(200, { status: "ready", dependencies: { postgres: "up", redis: "up" } });
    render(<StatusDashboard apiBaseUrl={BASE} />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("All systems ready"));

    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("down")));
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Backend unreachable"));
  });
});
