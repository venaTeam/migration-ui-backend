import { Router } from "express";
import { z } from "zod";
import { complianceFetch, sessionHeaders } from "../compliance/client.js";
import { config } from "../config.js";
import { GrafanaClient } from "../grafana/client.js";
import type { AuthenticatedRequest } from "../session/middleware.js";

const router = Router();

const loginSchema = z.object({
  grafana_username: z.string().min(1),
  grafana_password: z.string().min(1),
});

router.post("/login", async (req: AuthenticatedRequest, res) => {
  const parsed = loginSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "Bad Request", message: "Invalid login payload." });
    return;
  }

  const { grafana_username, grafana_password } = parsed.data;
  const grafanaUrl = config.grafanaUrl;

  // Validate credentials by calling EPIC-1's notification-policies endpoint.
  // It is lightweight and returns 401 on bad creds.
  try {
    const probe = await complianceFetch("/notification-policies?prefix=Appchi%20-%20", {
      method: "GET",
      headers: sessionHeaders(grafanaUrl, grafana_username, grafana_password),
    });

    if (probe.status === 401) {
      res.status(401).json({ error: "Unauthorized", message: "Invalid Grafana credentials." });
      return;
    }

    if (!probe.ok) {
      const body = await probe.text();
      res.status(probe.status).json({
        error: "Grafana API Error",
        message: `Could not reach Grafana via compliance service: ${body}`,
      });
      return;
    }

    // Collect the user's teams and org role from Grafana for folder permission checks.
    const grafanaClient = new GrafanaClient({
      baseUrl: grafanaUrl,
      username: grafana_username,
      password: grafana_password,
    });

    let teams: Array<{ id: number; name: string }> = [];
    let orgRole: string | undefined;
    try {
      const [userTeams, userOrgs] = await Promise.all([
        grafanaClient.getCurrentUserTeams(),
        grafanaClient.getCurrentUserOrgs(),
      ]);
      teams = userTeams;
      // Current org ONLY (roles are per-org): falling back to userOrgs[0]
      // could cache an Admin rank from an unrelated org and grant authority
      // wherever a role grant exists. If no current org, leave undefined —
      // the first folder-permission check re-fetches lazily.
      const currentOrg = userOrgs.find((org) => org.current === true);
      orgRole = currentOrg?.role as string | undefined;
    } catch (err) {
      // Do NOT cache the failure: leaving orgRole unset lets the first
      // permission check re-fetch lazily, so a transient outage at login
      // doesn't freeze reduced authority for the whole session TTL.
      console.warn("[login] could not fetch user teams/org role:", err);
    }

    req.session.grafana = {
      grafanaUrl,
      grafanaUsername: grafana_username,
      grafanaPassword: grafana_password,
      teams,
      orgRole,
    };

    req.session.save((err) => {
      if (err) {
        console.error("Session save failed:", err);
        res.status(500).json({ error: "Session Error", message: "Could not save session." });
        return;
      }
      res.json({ status: "ok" });
    });
  } catch (error) {
    res.status(502).json({
      error: "Compliance Service Error",
      message: error instanceof Error ? error.message : String(error),
    });
  }
});

router.post("/logout", (req: AuthenticatedRequest, res) => {
  req.session?.destroy(() => {
    res.clearCookie("sessionId");
    res.json({ status: "ok" });
  });
});

export default router;
