// k6 load test for the authenticated read paths and the public health endpoint.
//
//   docker run --rm -i --add-host=host.docker.internal:host-gateway ^
//     -v "%CD%\loadtests:/scripts" grafana/k6 run ^
//     -e API=http://host.docker.internal:8300 /scripts/api-reads.js
//
// It signs in through the development-only local identity provider (AUTH_PROVIDER=local, with the
// redirect origin in CORS_ORIGINS), creates a throwaway workspace with a handful of incidents, then
// applies a constant arrival rate to each path. Run it against a throwaway or development stack,
// never production. Rates and thresholds are knobs, not capacity claims: tune them to your target.
import crypto from "k6/crypto";
import encoding from "k6/encoding";
import http from "k6/http";
import { check, fail } from "k6";

const API = (__ENV.API || "http://localhost:8300").replace(/\/$/, "");
const ORIGIN = __ENV.REDIRECT_ORIGIN || "http://localhost:3300";
const USER = __ENV.LOCAL_USER || "owner";
const RATE = Number(__ENV.RATE || 30);
const DURATION = __ENV.DURATION || "30s";
const INCIDENTS = Number(__ENV.INCIDENTS || 20);

const scenario = (exec, rate) => ({
  executor: "constant-arrival-rate",
  exec,
  rate,
  timeUnit: "1s",
  duration: DURATION,
  preAllocatedVUs: 20,
  maxVUs: 100,
});

export const options = {
  scenarios: {
    incident_list: scenario("incidentList", RATE),
    incident_detail: scenario("incidentDetail", Math.max(1, Math.floor(RATE / 2))),
    health: scenario("health", Math.max(1, Math.floor(RATE / 2))),
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    checks: ["rate>0.99"],
    "http_req_duration{scenario:incident_list}": ["p(95)<500"],
    "http_req_duration{scenario:incident_detail}": ["p(95)<500"],
    "http_req_duration{scenario:health}": ["p(95)<300"],
  },
};

function signIn() {
  const verifier = encoding.b64encode(crypto.randomBytes(48), "rawurl");
  const challenge = crypto.sha256(verifier, "base64rawurl");
  const redirectUri = `${ORIGIN}/api/auth/callback`;
  const authorize = http.get(
    `${API}/api/v1/auth/local/authorize?redirect_uri=${encodeURIComponent(redirectUri)}` +
      `&state=load&nonce=load&code_challenge=${challenge}&login_hint=${USER}`,
    { redirects: 0 },
  );
  const match = /[?&]code=([^&]+)/.exec(authorize.headers["Location"] || "");
  if (!match) fail(`local sign-in failed (HTTP ${authorize.status}); needs AUTH_PROVIDER=local`);
  const token = http.post(`${API}/api/v1/auth/local/token`, {
    grant_type: "authorization_code",
    code: decodeURIComponent(match[1]),
    code_verifier: verifier,
    redirect_uri: redirectUri,
  });
  if (token.status !== 200) fail(`token exchange failed (HTTP ${token.status})`);
  return token.json("access_token");
}

export function setup() {
  const token = signIn();
  const headers = { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
  const suffix = `${Date.now()}`;
  const workspace = http.post(
    `${API}/api/v1/workspaces`,
    JSON.stringify({ name: `k6 ${suffix}`, slug: `k6-${suffix}` }),
    { headers },
  );
  if (workspace.status !== 201) fail(`workspace creation failed (HTTP ${workspace.status})`);
  const workspaceId = workspace.json("id");
  const incidentIds = [];
  for (let i = 0; i < INCIDENTS; i++) {
    const created = http.post(
      `${API}/api/v1/workspaces/${workspaceId}/incidents`,
      JSON.stringify({ title: `k6 incident ${i}`, severity: "sev3" }),
      { headers },
    );
    if (created.status !== 201) fail(`incident creation failed (HTTP ${created.status})`);
    incidentIds.push(created.json("id"));
  }
  return { headers, workspaceId, incidentIds };
}

export function incidentList(data) {
  const response = http.get(`${API}/api/v1/workspaces/${data.workspaceId}/incidents?limit=25`, {
    headers: data.headers,
  });
  check(response, {
    "list is 200": (r) => r.status === 200,
    "list has items": (r) => r.json("items").length > 0,
  });
}

export function incidentDetail(data) {
  const id = data.incidentIds[Math.floor(Math.random() * data.incidentIds.length)];
  const response = http.get(`${API}/api/v1/workspaces/${data.workspaceId}/incidents/${id}`, {
    headers: data.headers,
  });
  check(response, { "detail is 200": (r) => r.status === 200 });
}

export function health() {
  const response = http.get(`${API}/health/ready`);
  check(response, { "ready is 200": (r) => r.status === 200 });
}
