import type { NextConfig } from "next";

// Security headers. The CSP lets the page connect to itself, the sama API, and Supabase Auth.
// The API origin comes from NEXT_PUBLIC_SAMA_API_URL at build time.
function origin(value: string | undefined): string | null {
  if (!value) return null;
  try {
    return new URL(value).origin;
  } catch {
    return null;
  }
}

const isDev = process.env.NODE_ENV !== "production";
const apiOrigin = origin(process.env.NEXT_PUBLIC_SAMA_API_URL);

const csp = [
  "default-src 'self'",
  // Next.js writes small inline scripts and styles. Dev mode also needs eval.
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  `connect-src ${["'self'", apiOrigin, "https://*.supabase.co", isDev ? "ws:" : null]
    .filter(Boolean)
    .join(" ")}`,
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

const nextConfig: NextConfig = {
  poweredByHeader: false,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Content-Security-Policy", value: csp },
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Content-Type-Options", value: "nosniff" },
        ],
      },
    ];
  },
};

export default nextConfig;
