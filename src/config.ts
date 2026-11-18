import { z } from "zod";

const configSchema = z.object({
  port: z.coerce.number().default(3000),
  complianceServiceUrl: z.string().url().default("http://localhost:8000"),
  grafanaUrl: z.string().url().default("http://localhost:3000"),
  sessionSecret: z.string().min(16).default("change-me-in-production"),
  sessionTtlMinutes: z.coerce.number().default(60),
  logLevel: z.enum(["debug", "info", "warn", "error"]).default("info"),
  corsOrigin: z.string().default("http://localhost:5173"),
  // Where the migrations registry (SQLite file) lives. Must be on a
  // persistent volume in deployment (compose bind mount / OpenShift PVC).
  registryDataDir: z.string().default("./data"),
  grafanaAdminUsername: z.string().optional(),
  grafanaAdminPassword: z.string().optional(),
  // Admin-level service-account token (glsa_...). When set, takes precedence
  // over GRAFANA_ADMIN_USERNAME/PASSWORD for provisioning API calls.
  grafanaAdminToken: z.string().optional(),
});

export const config = configSchema.parse({
  port: process.env.UI_BACKEND_PORT,
  complianceServiceUrl: process.env.COMPLIANCE_SERVICE_URL,
  grafanaUrl: process.env.GRAFANA_URL,
  sessionSecret: process.env.SESSION_SECRET,
  sessionTtlMinutes: process.env.SESSION_TTL_MINUTES,
  logLevel: process.env.LOG_LEVEL,
  corsOrigin: process.env.CORS_ORIGIN,
  registryDataDir: process.env.REGISTRY_DATA_DIR,
  grafanaAdminUsername: process.env.GRAFANA_ADMIN_USERNAME,
  grafanaAdminPassword: process.env.GRAFANA_ADMIN_PASSWORD,
  grafanaAdminToken: process.env.GRAFANA_ADMIN_TOKEN,
});
