import "dotenv/config";
import express from "express";
import cors from "cors";
import session from "express-session";
import { config } from "./config.js";
import authRouter from "./routes/auth.js";
import standardRouter from "./routes/standard.js";
import backtestRouter from "./routes/backtest.js";
import policiesRouter from "./routes/policies.js";
import rulesRouter from "./routes/rules.js";
import type { AuthenticatedRequest } from "./session/middleware.js";
import { requireSession } from "./session/middleware.js";

const app = express();

// Trust OpenShift's reverse proxy so req.secure reflects the public scheme
app.set("trust proxy", 1);

app.use(
  cors({
    origin: config.corsOrigin,
    credentials: true,
  })
);
app.use(express.json());

app.use(
  session({
    name: "sessionId",
    secret: config.sessionSecret,
    resave: false,
    saveUninitialized: false,
    proxy: true,
    // Sliding expiry: rolling resets the cookie's maxAge on every response
    // that touches the session, so an actively-used session never expires —
    // the TTL measures IDLE time, not time-since-login. Without it a
    // fixed 30-min cookie logged users out mid-work.
    rolling: true,
    cookie: {
      httpOnly: true,
      secure: "auto",
      sameSite: "lax",
      path: "/",
      maxAge: config.sessionTtlMinutes * 60 * 1000,
    },
  })
);


app.get("/api/health", (_req, res) => {
  res.json({ status: "ok" });
});

app.use("/api/auth", authRouter);
app.use("/api/standard", requireSession, standardRouter);
app.use("/api/notification-policies", requireSession, policiesRouter);
app.use("/api/rules", requireSession, rulesRouter);
app.use("/api/backtest", requireSession, backtestRouter);

app.listen(config.port, () => {
  console.log(`UI backend listening on port ${config.port}`);
  console.log(`Compliance service: ${config.complianceServiceUrl}`);
});
