// Cloudflare Pages Advanced Mode — API proxy + SPA routing
// CF Pages _redirects cannot proxy to external URLs (Netlify-only feature)
// This worker handles: API proxy to Railway, /app alias, security headers

const API_ORIGIN = "https://fsbo-api-production.up.railway.app";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    // --- API proxy: /api/* → Railway ---
    if (path.startsWith("/api/")) {
      const apiUrl = API_ORIGIN + path + url.search;
      const apiReq = new Request(apiUrl, {
        method: request.method,
        headers: request.headers,
        body: request.method !== "GET" && request.method !== "HEAD" ? request.body : undefined,
      });
      const apiResp = await fetch(apiReq);
      const headers = new Headers(apiResp.headers);
      headers.set("Access-Control-Allow-Origin", url.origin);
      return new Response(apiResp.body, {
        status: apiResp.status,
        headers,
      });
    }

    // --- Legacy redirects ---
    if (path === "/listing-tracker.html" || path === "/listing-tracker") {
      return Response.redirect(url.origin + "/app", 301);
    }
    if (path === "/login" || path === "/signup") {
      return Response.redirect(url.origin + "/app", 301);
    }

    // --- /app alias → listing-tracker.html ---
    if (path === "/app" || path.startsWith("/app/")) {
      const asset = await env.ASSETS.fetch(new URL("/listing-tracker.html", url.origin));
      return addSecurityHeaders(asset);
    }

    // --- Serve static assets ---
    const response = await env.ASSETS.fetch(request);
    if (response.status !== 404) {
      return addSecurityHeaders(response);
    }

    // --- Clean URL fallback for nested HTML pages ---
    // Cloudflare Pages will not resolve /markets/charlotte -> /markets/charlotte.html
    // automatically in this worker path, so try a .html suffix before giving up.
    const hasExtension = path.split("/").pop()?.includes(".");
    if (!hasExtension && !path.endsWith("/")) {
      const htmlResponse = await env.ASSETS.fetch(new URL(`${path}.html`, url.origin));
      if (htmlResponse.status !== 404) {
        return addSecurityHeaders(htmlResponse);
      }
    }

    return addSecurityHeaders(response);
  },
};

function addSecurityHeaders(response) {
  const headers = new Headers(response.headers);
  headers.set("X-Frame-Options", "SAMEORIGIN");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  headers.set("Permissions-Policy", "camera=(), microphone=(), geolocation=()");
  return new Response(response.body, {
    status: response.status,
    headers,
  });
}
