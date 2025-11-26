import type { NextConfig } from "next";
import fs from "fs";
import path from "path";

/**
 * Load shared backend configuration from backend/backend.env so that
 * both backend (Python) and frontend (Next.js) use the same single
 * source of truth.
 *
 * This function is executed at build/startup time on the Node side only.
 */
function loadBackendEnv(): Record<string, string> {
  const envPath = path.join(__dirname, "..", "backend", "backend.env");
  const result: Record<string, string> = {};

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
    // Non-fatal: just log once for diagnostics
    console.warn("⚠️  Failed to load backend/backend.env in next.config.ts:", e);
  }

  return result;
}

const backendEnv = loadBackendEnv();

// Resolve backend base URL once, avoiding accidental use of the Next.js PORT (3000).
// Priority:
// 1) NEXT_PUBLIC_API_URL (explicit frontend override)
// 2) API_BASE_URL from environment/backend.env (shared with Python backend)
// 3) fallback to Docker-style service name "http://backend:8000"
const rawApiBase =
  process.env.NEXT_PUBLIC_API_URL ||
  process.env.API_BASE_URL ||
  backendEnv.API_BASE_URL ||
  "http://backend:8000";

const apiBase = rawApiBase.replace(/\/$/, "");

// Debug logging for build diagnostics
console.log("=== Next.js Config Debug ===");
console.log("NEXT_PUBLIC_API_URL:", process.env.NEXT_PUBLIC_API_URL);
console.log("API_BASE_URL (env):", process.env.API_BASE_URL);
console.log("API_BASE_URL (backend.env):", backendEnv.API_BASE_URL);
console.log("Resolved apiBase:", apiBase);
console.log("==========================");

const nextConfig: NextConfig = {
  // 启用standalone输出模式，用于Docker部署
  output: 'standalone',

  eslint: {
    ignoreDuringBuilds: true,
  },
  typescript: {
    ignoreBuildErrors: true,
  },

  // Proxy frontend '/api/*' calls to the backend, preserving existing '/api' namespaces used by the backend.
  async rewrites() {
    const rules = [
      // Keep backend endpoints that are already under /api working
      { source: "/api/awb/:path*", destination: `${apiBase}/api/awb/:path*` },
      { source: "/api/admin/:path*", destination: `${apiBase}/api/admin/:path*` },

      // Map remaining '/api/*' calls to backend root-mounted routes
      { source: "/api/:path*", destination: `${apiBase}/:path*` },

      // File preview passthrough (non-API)
      { source: "/files/:fileId/preview", destination: `${apiBase}/files/:fileId` },
    ];

    console.log("=== Rewrites Configuration ===");
    console.log(JSON.stringify(rules, null, 2));
    console.log("=============================");

    return rules;
  },

  // Add redirects for reorganized routes
  async redirects() {
    return [
      // Move OneDrive Sync under Admin
      {
        source: "/awb/sync",
        destination: "/admin/awb/sync",
        permanent: true,
      },
    ];
  },
};

export default nextConfig;
