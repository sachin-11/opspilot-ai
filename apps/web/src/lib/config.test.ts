import { describe, expect, it } from "vitest";

import { getApiBaseUrl } from "./config";

describe("getApiBaseUrl", () => {
  it("returns a valid URL without trailing slash", () => {
    expect(getApiBaseUrl({ NEXT_PUBLIC_API_BASE_URL: "http://localhost:8300/" })).toBe(
      "http://localhost:8300",
    );
  });

  it("fails fast when missing", () => {
    expect(() => getApiBaseUrl({})).toThrow(/NEXT_PUBLIC_API_BASE_URL/);
  });

  it("rejects non-http URLs", () => {
    expect(() => getApiBaseUrl({ NEXT_PUBLIC_API_BASE_URL: "ftp://x.example" })).toThrow();
    expect(() => getApiBaseUrl({ NEXT_PUBLIC_API_BASE_URL: "not a url" })).toThrow();
  });
});
