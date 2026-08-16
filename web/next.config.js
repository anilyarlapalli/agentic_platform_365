/** @type {import('next').NextConfig} */

// Every browser request goes to this Next server's own origin and is rewritten
// to the FastAPI app. That is deliberate, and it is the reason `create_app` has
// no CORS middleware: same-origin requests never trigger a preflight, so the
// API's default-deny surface does not need an `OPTIONS` hole punched through it.
// Pointing the browser straight at :8100 instead would require adding CORS —
// see ROLLOUT.md for why that was rejected.
const production = process.env.NODE_ENV === "production";
const contentSecurityPolicy = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${production ? "" : " 'unsafe-eval'"}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "frame-ancestors 'none'",
  "form-action 'self'",
  ...(production ? ["upgrade-insecure-requests"] : []),
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: contentSecurityPolicy },
  { key: "Referrer-Policy", value: "no-referrer" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=(), payment=()",
  },
  ...(production
    ? [
        {
          key: "Strict-Transport-Security",
          value: "max-age=63072000; includeSubDomains; preload",
        },
      ]
    : []),
];

const nextConfig = {
  reactStrictMode: true,
  output: "standalone",
  experimental: {
    // A cold `mode: "graph"` query builds the knowledge graph before it answers.
    // The dev server's ~30s default proxy timeout cuts that off and surfaces in
    // the browser as a 500 `socket hang up`, which reads like a backend crash
    // rather than a timeout. Five minutes covers a cold graph build.
    proxyTimeout: 300_000,
  },
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  async rewrites() {
    const apiOrigin =
      process.env.API_ORIGIN ||
      process.env.NEXT_PUBLIC_API_ORIGIN ||
      "http://127.0.0.1:8100";
    return [
      {
        source: "/api/:path*",
        destination: `${apiOrigin}/api/:path*`,
      },
      // The auth and health routers carry no `/api` prefix on the API — the
      // real paths are `/auth/login` and `/health`, and PUBLIC_PATHS lists them
      // in that form. They need their own rules; folding them under /api would
      // rewrite to paths the server does not serve.
      {
        source: "/auth/:path*",
        destination: `${apiOrigin}/auth/:path*`,
      },
      {
        source: "/health",
        destination: `${apiOrigin}/health`,
      },
      {
        source: "/health/:path*",
        destination: `${apiOrigin}/health/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
