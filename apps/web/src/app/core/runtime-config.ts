/**
 * Where the browser finds the API, decided at load time rather than at build
 * time.
 *
 * In development the Angular dev server proxies `/api` to the local FastAPI
 * process, so a relative path is correct. In production the app is static
 * files on Cloudflare Pages and the API is a Cloud Run service behind
 * `api.<domain>`, so a relative path resolves to Pages and returns the index
 * page for every call. That failure is quiet and confusing: requests get a 200
 * with HTML in it, so the app reports a parse error rather than a routing
 * mistake.
 *
 * Reading the origin from a small file the deploy writes, rather than baking it
 * into the bundle, means one build artifact is promoted from staging to
 * production unchanged. A bundle that hardcodes its environment has to be
 * rebuilt to be repointed, and a rebuild is a different artifact than the one
 * that passed the tests.
 *
 * A missing or malformed file falls back to the relative path. Configuration
 * that fails should degrade to the development default, not to a blank screen.
 */

export interface RuntimeConfig {
  /** Absolute origin plus prefix in production, relative prefix in dev. */
  readonly apiBase: string;
  readonly environment: string;
}

const FALLBACK: RuntimeConfig = {
  apiBase: '/api/v1',
  environment: 'local',
};

let active: RuntimeConfig = FALLBACK;

export function runtimeConfig(): RuntimeConfig {
  return active;
}

/** Fetched once before the app bootstraps. Never throws. */
export async function loadRuntimeConfig(): Promise<void> {
  try {
    // Rooted, not relative: a deep link like /review/abc would otherwise look
    // for /review/config.json.
    const response = await fetch('/config.json', { cache: 'no-store' });
    if (!response.ok) return;

    const parsed = (await response.json()) as Partial<RuntimeConfig>;
    active = {
      apiBase: typeof parsed.apiBase === 'string' && parsed.apiBase ? parsed.apiBase : FALLBACK.apiBase,
      environment:
        typeof parsed.environment === 'string' && parsed.environment
          ? parsed.environment
          : FALLBACK.environment,
    };
  } catch {
    // Offline, blocked, or serving something that is not JSON. The default is
    // still a working configuration for local development.
  }
}
