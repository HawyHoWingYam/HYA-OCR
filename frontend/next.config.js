const fs = require("fs");
const path = require("path");

/**
 * Load shared backend configuration from backend/backend.env so that
 * both backend (Python) and frontend (Next.js) use the same single
 * source of truth.
 *
 * This runs at build/startup time on the Node side only.
 */
function loadBackendEnv() {
  const envPath = path.join(__dirname, "..", "backend", "backend.env");
  const result = {};

  try {
    if (fs.existsSync(envPath)) {
      const content = fs.readFileSync(envPath, "utf8");
      for (const rawLine of content.split(/\r?\n/)) {
        const line = rawLine.trim();
        if (!line || line.startsWith("#")) continue;
        const idx = line.indexOf("=");
        if (idx === -1) continue;
        const key = line.slice(0, idx).trim();
        const value = line.slice(idx + 1).trim();
        if (!key) continue;

        result[key] = value;

        // Populate process.env for downstream code if not already set
        if (!(key in process.env)) {
          process.env[key] = value;
        }
      }
    }
  } catch (e) {
    console.warn("⚠️  Failed to load backend/backend.env in next.config.js:", e);
  }

  return result;
}

const backendEnv = loadBackendEnv();

// Priority for backend base URL:
// 1) NEXT_PUBLIC_API_URL (explicit override for frontend)
// 2) API_BASE_URL from environment/backend.env (shared with backend)
// 3) fallback to Docker-style service name "http://backend:8000"
const rawApiHost =
  process.env.NEXT_PUBLIC_API_URL ||
  process.env.API_BASE_URL ||
  backendEnv.API_BASE_URL ||
  "http://backend:8000";

const apiHost = rawApiHost.replace(/\/$/, "");

// Expose resolved API host to the browser so client-side fetches can hit backend directly
if (!process.env.NEXT_PUBLIC_API_URL) {
  process.env.NEXT_PUBLIC_API_URL = apiHost;
}

console.log("=== next.config.js Debug ===");
console.log("NEXT_PUBLIC_API_URL:", process.env.NEXT_PUBLIC_API_URL);
console.log("API_BASE_URL (env):", process.env.API_BASE_URL);
console.log("API_BASE_URL (backend.env):", backendEnv.API_BASE_URL);
console.log("Resolved apiHost:", apiHost);
console.log("===========================");

/** @type {import('next').NextConfig} */
const nextConfig = {
  // 启用standalone输出模式，用于Docker部署
  output: "standalone",

  eslint: {
    ignoreDuringBuilds: true,
  },
  typescript: {
    // This will allow the build to continue even with TypeScript errors
    ignoreBuildErrors: true,
  },

  // Proxy frontend '/api/*' calls to the backend.
  // Note: The backend mixes routes: most are mounted at '/', but some are under '/api' (e.g. awb, admin/usage).
  // We special‑case those first, then fall back to '/' for the rest to avoid widespread 404s.
  async rewrites() {
    return [
      // Keep backend endpoints that are already under /api working
      { source: "/api/awb/:path*", destination: `${apiHost}/api/awb/:path*` },
      { source: "/api/admin/:path*", destination: `${apiHost}/api/admin/:path*` },

      // Map remaining '/api/*' calls to backend root-mounted routes (e.g., /orders, /document-types, /jobs, ...)
      { source: "/api/:path*", destination: `${apiHost}/:path*` },

      // File preview passthrough (non-API)
      { source: "/files/:fileId/preview", destination: `${apiHost}/files/:fileId` },
    ];
  },

  // Additional CORS configuration
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          {
            key: "Access-Control-Allow-Origin",
            value: "*",
          },
          {
            key: "Access-Control-Allow-Methods",
            value: "GET, POST, PUT, DELETE, OPTIONS",
          },
          {
            key: "Access-Control-Allow-Headers",
            value: "Content-Type, Authorization",
          },
        ],
      },
    ];
  },
};

module.exports = nextConfig;
