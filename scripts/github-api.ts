/**
 * Shared GitHub API client with rate-limit handling and exponential backoff.
 *
 * All scripts that call the GitHub REST API should use `githubRequest` from
 * this module instead of calling `fetch` directly.  The wrapper:
 *
 *  1. Detects 403/429 rate-limit responses and waits until the reset window
 *     indicated by the `x-ratelimit-reset` header (or a sensible default).
 *  2. Retries transient errors (5xx, network failures) with exponential
 *     backoff and jitter (up to `MAX_RETRIES` attempts).
 *  3. Logs remaining quota every time it drops below a configurable threshold
 *     so operators can spot problems before they become outages.
 */

const MAX_RETRIES = 5;
const BASE_DELAY_MS = 1_000;
const RATE_LIMIT_WARN_THRESHOLD = 50;

/** Compute delay with exponential backoff + jitter. */
function backoffDelay(attempt: number): number {
  const exponential = BASE_DELAY_MS * Math.pow(2, attempt);
  const jitter = Math.random() * BASE_DELAY_MS;
  return exponential + jitter;
}

/** Sleep helper. */
function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Shared GitHub REST API request function with rate-limit handling.
 *
 * @param endpoint   Path relative to https://api.github.com (e.g. `/repos/o/r/issues`)
 * @param token      GitHub bearer token
 * @param method     HTTP method (default GET)
 * @param body       Optional JSON-serialisable body
 * @param userAgent  Value for the User-Agent header (default "github-api-client")
 */
export async function githubRequest<T>(
  endpoint: string,
  token: string,
  method: string = "GET",
  body?: unknown,
  userAgent: string = "github-api-client",
): Promise<T> {
  let lastError: Error | null = null;

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      const response = await fetch(`https://api.github.com${endpoint}`, {
        method,
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github.v3+json",
          "User-Agent": userAgent,
          ...(body ? { "Content-Type": "application/json" } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });

      // --- Rate-limit awareness ---
      const remaining = response.headers.get("x-ratelimit-remaining");
      const resetHeader = response.headers.get("x-ratelimit-reset");

      if (remaining !== null) {
        const remainingNum = parseInt(remaining, 10);
        if (remainingNum <= RATE_LIMIT_WARN_THRESHOLD) {
          const resetDate = resetHeader
            ? new Date(parseInt(resetHeader, 10) * 1000)
            : null;
          console.warn(
            `[rate-limit] ${remainingNum} requests remaining` +
              (resetDate ? ` — resets at ${resetDate.toISOString()}` : ""),
          );
        }
      }

      // --- Rate-limited (primary or secondary) ---
      if (response.status === 403 || response.status === 429) {
        const retryAfterHeader = response.headers.get("retry-after");
        let waitMs: number;

        if (retryAfterHeader) {
          // Retry-After is in seconds
          waitMs = parseInt(retryAfterHeader, 10) * 1000;
        } else if (resetHeader) {
          const resetTime = parseInt(resetHeader, 10) * 1000;
          waitMs = Math.max(resetTime - Date.now(), 1_000);
        } else {
          waitMs = backoffDelay(attempt);
        }

        // Cap the wait at 5 minutes to avoid indefinite hangs in CI
        waitMs = Math.min(waitMs, 5 * 60 * 1000);

        console.warn(
          `[rate-limit] Hit rate limit (${response.status}) on ${method} ${endpoint}. ` +
            `Waiting ${Math.ceil(waitMs / 1000)}s before retry ${attempt + 1}/${MAX_RETRIES}...`,
        );
        await sleep(waitMs);
        continue;
      }

      // --- Server errors (retriable) ---
      if (response.status >= 500) {
        const text = await response.text();
        lastError = new Error(
          `GitHub API server error: ${response.status} ${text}`,
        );
        if (attempt < MAX_RETRIES) {
          const delay = backoffDelay(attempt);
          console.warn(
            `[retry] Server error ${response.status} on ${method} ${endpoint}. ` +
              `Retrying in ${Math.ceil(delay / 1000)}s (${attempt + 1}/${MAX_RETRIES})...`,
          );
          await sleep(delay);
          continue;
        }
        throw lastError;
      }

      // --- 404 returns empty object (matches existing sweep.ts behaviour) ---
      if (response.status === 404) {
        return {} as T;
      }

      // --- Other client errors are terminal ---
      if (!response.ok) {
        const text = await response.text();
        throw new Error(
          `GitHub API request failed: ${response.status} ${response.statusText} — ${text}`,
        );
      }

      return response.json();
    } catch (err: unknown) {
      // Network errors (ECONNRESET, DNS failures, etc.) are retriable
      if (
        err instanceof TypeError ||
        (err instanceof Error && err.message.includes("fetch"))
      ) {
        lastError = err instanceof Error ? err : new Error(String(err));
        if (attempt < MAX_RETRIES) {
          const delay = backoffDelay(attempt);
          console.warn(
            `[retry] Network error on ${method} ${endpoint}: ${lastError.message}. ` +
              `Retrying in ${Math.ceil(delay / 1000)}s (${attempt + 1}/${MAX_RETRIES})...`,
          );
          await sleep(delay);
          continue;
        }
      }
      throw err;
    }
  }

  throw lastError ?? new Error(`GitHub API request failed after ${MAX_RETRIES} retries`);
}
