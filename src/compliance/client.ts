
import { config } from "../config.js";

export interface ComplianceHeaders {
  "X-Grafana-Url": string;
  "X-Grafana-Username": string;
  "X-Grafana-Password": string;
}

export function complianceUrl(path: string): string {
  const base = config.complianceServiceUrl.replace(/\/$/, "");
  return `${base}/api/v1${path}`;
}

export async function complianceFetch(
  path: string,
  options: RequestInit & { headers?: Record<string, string> } = {}
): Promise<Response> {
  const url = complianceUrl(path);
  return fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...options.headers,
    },
  });
}

export function sessionHeaders(
  grafanaUrl: string,
  grafanaUsername: string,
  grafanaPassword: string
): Record<string, string> {
  return {
    "X-Grafana-Url": grafanaUrl,
    "X-Grafana-Username": grafanaUsername,
    "X-Grafana-Password": grafanaPassword,
  };
}
