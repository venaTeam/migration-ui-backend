import { Router } from "express";
import { z } from "zod";
import { complianceFetch, sessionHeaders } from "../compliance/client.js";
import type { AuthenticatedRequest } from "../session/middleware.js";

const router = Router();

const backtestSchema = z.object({
  rule_query: z.string().min(1),
  threshold: z.number(),
  condition: z.enum(["gt", "gte", "lt", "lte", "eq", "neq"]).default("gt"),
  for_duration_seconds: z.number().int().default(0),
  window_hours: z.number().int().min(1).max(168).default(24),
});

router.post("/", async (req: AuthenticatedRequest, res) => {
  const parsed = backtestSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "Bad Request", message: "Invalid backtest payload." });
    return;
  }

  try {
    const response = await complianceFetch("/backtest", {
      method: "POST",
      headers: sessionHeaders(req.grafanaUrl!, req.grafanaUsername!, req.grafanaPassword!),
      body: JSON.stringify(parsed.data),
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
