import { z } from "zod";

const envSchema = z.object({
  NEXT_PUBLIC_API_BASE_URL: z
    .string({ error: "NEXT_PUBLIC_API_BASE_URL is required" })
    .url("NEXT_PUBLIC_API_BASE_URL must be a valid URL")
    .refine((value) => /^https?:\/\//.test(value), "NEXT_PUBLIC_API_BASE_URL must be http(s)"),
});

/** Validates the public environment and returns the API base URL without a trailing slash. */
export function getApiBaseUrl(
  // Referenced statically so Next.js can inline the value into the client bundle.
  env: Record<string, string | undefined> = {
    NEXT_PUBLIC_API_BASE_URL: process.env.NEXT_PUBLIC_API_BASE_URL,
  },
): string {
  const parsed = envSchema.safeParse(env);
  if (!parsed.success) {
    const details = parsed.error.issues.map((issue) => issue.message).join("; ");
    throw new Error(`Invalid frontend configuration: ${details}`);
  }
  return parsed.data.NEXT_PUBLIC_API_BASE_URL.replace(/\/+$/, "");
}
