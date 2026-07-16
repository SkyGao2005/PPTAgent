const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "")

const ABSOLUTE_URL = /^[a-z][a-z\d+.-]*:/i

export function resolveWithApiBase(url: string, baseUrl: string): string {
  if (!url || ABSOLUTE_URL.test(url)) {
    return url
  }
  const normalizedBase = baseUrl.replace(/\/$/, "")
  if (!normalizedBase) {
    return url.startsWith("/") ? url : `/${url}`
  }
  return `${normalizedBase}${url.startsWith("/") ? "" : "/"}${url}`
}

/** Resolve REST, SSE, preview, and export URLs against the configured API origin. */
export function resolveApiUrl(url: string): string {
  return resolveWithApiBase(url, API_BASE_URL)
}
