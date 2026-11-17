import https from "https";
import http from "http";
import { URL } from "url";
import { config } from "../config.js";
import { GrafanaAPIError, GrafanaAuthError, RuleNotFoundError } from "./errors.js";

export interface GrafanaCredential {
  baseUrl: string;
  username: string;
  password: string;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: string;
  disableProvenance?: boolean;
}

export function adminGrafanaClient(baseUrl: string): GrafanaClient | undefined {
  if (!config.grafanaAdminUsername || !config.grafanaAdminPassword) {
    return undefined;
  }
  return new GrafanaClient({
    baseUrl,
    username: config.grafanaAdminUsername,
    password: config.grafanaAdminPassword,
  });
}

export class GrafanaClient {
  private credentials: GrafanaCredential;
  private timeout: number;
  private rejectUnauthorized: boolean;

  constructor(credentials: GrafanaCredential, timeout = 15000) {
    this.credentials = credentials;
    this.timeout = timeout;
    this.rejectUnauthorized = process.env.GRAFANA_INSECURE_TLS !== "true";
  }

  private request(
    path: string,
    options: RequestOptions = {}
  ): Promise<{ statusCode: number; body: string }> {
    const url = new URL(path, this.credentials.baseUrl.replace(/\/$/, "") + "/");
    const auth = Buffer.from(`${this.credentials.username}:${this.credentials.password}`).toString(
      "base64"
    );

    const requestOptions: https.RequestOptions = {
      hostname: url.hostname,
      port: url.port || (url.protocol === "https:" ? 443 : 80),
      path: `${url.pathname}${url.search}`,
      method: options.method || "GET",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        Authorization: `Basic ${auth}`,
        ...(options.disableProvenance ? { "X-Disable-Provenance": "true" } : {}),
      },
      rejectUnauthorized: this.rejectUnauthorized,
      timeout: this.timeout,
    };

    return new Promise((resolve, reject) => {
      const lib = url.protocol === "https:" ? https : http;
      const req = lib.request(requestOptions, (res) => {
        let data = "";
        res.on("data", (chunk) => {
          data += chunk;
        });
        res.on("end", () => {
          resolve({ statusCode: res.statusCode || 0, body: data });
        });
      });

      req.on("error", (err) => {
        reject(new GrafanaAPIError(`Could not reach Grafana: ${err.message}`, 502));
      });

      req.on("timeout", () => {
        req.destroy();
        reject(new GrafanaAPIError(`Request to Grafana timed out after ${this.timeout}ms`, 504));
      });

      if (options.body) {
        req.write(options.body);
      }
      req.end();
    });
  }

  async getRule(uid: string): Promise<Record<string, unknown>> {
    const { statusCode, body } = await this.request(
      `/api/v1/provisioning/alert-rules/${encodeURIComponent(uid)}`
    );
    if (statusCode === 404) {
      throw new RuleNotFoundError(uid);
    }
    if (statusCode === 401) {
      throw new GrafanaAuthError();
    }
    if (statusCode < 200 || statusCode >= 300) {
      throw new GrafanaAPIError(`Failed to fetch rule: ${body}`, statusCode);
    }
    return JSON.parse(body) as Record<string, unknown>;
  }

  async createRule(rule: Record<string, unknown>): Promise<Record<string, unknown>> {
    const { statusCode, body } = await this.request("/api/v1/provisioning/alert-rules", {
      method: "POST",
      body: JSON.stringify(rule),
    });
    if (statusCode === 401) {
      throw new GrafanaAuthError();
    }
    if (statusCode < 200 || statusCode >= 300) {
      throw new GrafanaAPIError(`Failed to create rule: ${body}`, statusCode);
    }
    return JSON.parse(body) as Record<string, unknown>;
  }

  async createEditableRule(rule: Record<string, unknown>): Promise<Record<string, unknown>> {
    const { statusCode, body } = await this.request("/api/v1/provisioning/alert-rules", {
      method: "POST",
      body: JSON.stringify(rule),
      disableProvenance: true,
    });
    if (statusCode === 401) {
      throw new GrafanaAuthError();
    }
    if (statusCode < 200 || statusCode >= 300) {
      throw new GrafanaAPIError(`Failed to create editable rule: ${body}`, statusCode);
    }
    return JSON.parse(body) as Record<string, unknown>;
  }

  async listRules(): Promise<Array<Record<string, unknown>>> {
    const { statusCode, body } = await this.request("/api/v1/provisioning/alert-rules");
    if (statusCode === 401) {
      throw new GrafanaAuthError();
    }
    if (statusCode < 200 || statusCode >= 300) {
      throw new GrafanaAPIError(`Failed to list rules: ${body}`, statusCode);
    }
    return JSON.parse(body) as Array<Record<string, unknown>>;
  }

  async listRulesInFolder(folderUid: string): Promise<Array<Record<string, unknown>>> {
    const allRules = await this.listRules();
    return allRules.filter((rule) => rule.folderUID === folderUid);
  }

  async updateRule(
    uid: string,
    rule: Record<string, unknown>
  ): Promise<Record<string, unknown>> {
    const { statusCode, body } = await this.request(
      `/api/v1/provisioning/alert-rules/${encodeURIComponent(uid)}`,
      {
        method: "PUT",
        body: JSON.stringify(rule),
        disableProvenance: true,
      }
    );
    if (statusCode === 401) {
      throw new GrafanaAuthError();
    }
    if (statusCode === 404) {
      throw new RuleNotFoundError(uid);
    }
    if (statusCode < 200 || statusCode >= 300) {
      throw new GrafanaAPIError(`Failed to update rule: ${body}`, statusCode);
    }
    return JSON.parse(body) as Record<string, unknown>;
  }

  async deleteRule(uid: string): Promise<void> {
    const { statusCode, body } = await this.request(
      `/api/v1/provisioning/alert-rules/${encodeURIComponent(uid)}`,
      {
        method: "DELETE",
      }
    );
    if (statusCode === 401) {
      throw new GrafanaAuthError();
    }
    if (statusCode === 404) {
      throw new RuleNotFoundError(uid);
    }
    if (statusCode < 200 || statusCode >= 300) {
      throw new GrafanaAPIError(`Failed to delete rule: ${body}`, statusCode);
    }
  }

  async getCurrentUserOrgs(): Promise<Array<Record<string, unknown>>> {
    const { statusCode, body } = await this.request("/api/user/orgs");
    if (statusCode === 401) {
      throw new GrafanaAuthError();
    }
    if (statusCode < 200 || statusCode >= 300) {
      throw new GrafanaAPIError(`Failed to fetch user orgs: ${body}`, statusCode);
    }
    return JSON.parse(body) as Array<Record<string, unknown>>;
  }

  async getCurrentUserTeams(): Promise<Array<{ id: number; name: string }>> {
    const { statusCode, body } = await this.request("/api/user/teams");
    if (statusCode === 401) {
      throw new GrafanaAuthError();
    }
    if (statusCode < 200 || statusCode >= 300) {
      throw new GrafanaAPIError(`Failed to fetch user teams: ${body}`, statusCode);
    }
    return JSON.parse(body) as Array<{ id: number; name: string }>;
  }

  async getFolderPermissions(folderUid: string): Promise<Array<Record<string, unknown>>> {
    const { statusCode, body } = await this.request(`/api/folders/${encodeURIComponent(folderUid)}/permissions`);
    if (statusCode === 401) {
      throw new GrafanaAuthError();
    }
    if (statusCode < 200 || statusCode >= 300) {
      throw new GrafanaAPIError(`Failed to fetch folder permissions: ${body}`, statusCode);
    }
    const parsed = JSON.parse(body) as { items?: Array<Record<string, unknown>> } | Array<Record<string, unknown>>;
    return Array.isArray(parsed) ? parsed : parsed.items || [];
  }
}
