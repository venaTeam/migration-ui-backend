import type { Request, Response, NextFunction } from "express";
import { config } from "../config.js";

export interface GrafanaSession {
  grafanaUrl: string;
  grafanaUsername: string;
  grafanaPassword: string;
  orgRole?: string;
  teams?: Array<{ id: number; name: string }>;
}

declare module "express-session" {
  interface SessionData {
    grafana?: GrafanaSession;
  }
}

export interface AuthenticatedRequest extends Request {
  grafanaUrl?: string;
  grafanaUsername?: string;
  grafanaPassword?: string;
  grafanaOrgRole?: string;
  grafanaTeams?: Array<{ id: number; name: string }>;
}

export function requireSession(req: AuthenticatedRequest, res: Response, next: NextFunction): void {
  const grafana = req.session?.grafana;
  if (!grafana) {
    res.status(401).json({ error: "Unauthorized", message: "Session expired or invalid." });
    return;
  }

  req.grafanaUrl = grafana.grafanaUrl || config.grafanaUrl;
  req.grafanaUsername = grafana.grafanaUsername;
  req.grafanaPassword = grafana.grafanaPassword;
  req.grafanaOrgRole = grafana.orgRole;
  req.grafanaTeams = grafana.teams;
  next();
}
