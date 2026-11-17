import { Router } from "express";
import { complianceFetch, sessionHeaders } from "../compliance/client.js";
import type { AuthenticatedRequest } from "../session/middleware.js";

const router = Router();

router.get("/", async (req: AuthenticatedRequest, res) => {
  const prefix = typeof req.query.prefix === "string" ? req.query.prefix : "Appchi - ";
  try {
    const response = await complianceFetch(`/notification-policies?prefix=${encodeURIComponent(prefix)}`, {
      method: "GET",
      headers: sessionHeaders(req.grafanaUrl!, req.grafanaUsername!, req.grafanaPassword!),
    });
    const data = await response.json();
    res.status(response.status).json(data);
  } catch (error) {
    res.status(502).json({
      error: "Compliance Service Error",
      message: error instanceof Error ? error.message : String(error),
    });
  }
});

export default router;
