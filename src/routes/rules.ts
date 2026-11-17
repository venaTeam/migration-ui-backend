import { Router, type Response } from "express";
import { z } from "zod";
import { complianceFetch, sessionHeaders } from "../compliance/client.js";
import { adminGrafanaClient, GrafanaClient } from "../grafana/client.js";
import { GrafanaAPIError, GrafanaAuthError, RuleNotFoundError } from "../grafana/errors.js";
import type { AuthenticatedRequest } from "../session/middleware.js";
import { recordMigration, recordViewDelete, latestMigrationForOriginal } from "../registry/store.js";
import { seedFolder, computeMigratedFlags } from "../registry/flags.js";

const router = Router();

// Permission levels from Grafana folder permissions API.
const FOLDER_PERMISSION_EDIT = 2;

// Grafana built-in role ranks (Viewer < Editor < Admin). A folder's built-in
// role grant means "this role and anything above it", so an org Admin must
// pass an Editor-only grant — exact string equality would wrongly reject it.
const BUILT_IN_ROLE_RANK: Record<string, number> = {
  viewer: 1,
  editor: 2,
  admin: 3,
};

/**
 * Re-fetch the user's CURRENT-org role with their own credentials and cache
 * it back into the session. Used when the login-time org fetch failed or
 * never recorded a role — the fresh value replaces the frozen miss instead
 * of leaving reduced authority in place until the session expires.
 * Returns undefined on failure (treated as "no role", never as error).
 */
async function refreshOrgRole(
  adminClient: GrafanaClient,
  req: AuthenticatedRequest
): Promise<string | undefined> {
  if (!req.session?.grafana) return undefined;
  try {
    const userClient = new GrafanaClient({
      baseUrl: req.grafanaUrl!,
      username: req.grafanaUsername!,
      password: req.grafanaPassword!,
    });
    const userOrgs = await userClient.getCurrentUserOrgs();
    // Current org ONLY: roles are per-org, so an Admin rank in an unrelated
    // org must never grant authority in the org the permission is checked in.
    const currentOrg = userOrgs.find((org) => org.current === true);
    const role = currentOrg?.role as string | undefined;
    req.session.grafana.orgRole = role;
    req.grafanaOrgRole = role;
    req.session.save(() => {});
    return role;
  } catch (err) {
    console.warn("[permission] org-role refresh failed — treating as no role:", err);
    return undefined;
  }
}

async function checkFolderPermission(
  adminClient: GrafanaClient,
  folderUid: string,
  req: AuthenticatedRequest
): Promise<boolean> {
  const items = await adminClient.getFolderPermissions(folderUid);
  const teamIds = new Set(req.grafanaTeams?.map((t) => t.id) ?? []);
  // Grafana roles are PER-ORG and the session's orgRole is a snapshot from
  // login time — if that fetch failed (or Grafana says the role changed),
  // the user's own credentials are the fresh source of truth: the caller
  // uses an admin client but the /api/user endpoints validate the session
  // credentials themselves. Lazy re-fetch on the first permission check
  // (only once per session) keeps a transient login-time outage from
  // freezing reduced authority until session expiry.
  let orgRole = req.grafanaOrgRole;
  if (orgRole === undefined) {
    const refreshed = await refreshOrgRole(adminClient, req);
    orgRole = refreshed;
  }

  for (const item of items) {
    const permission = item.permission as number;
    if (permission < FOLDER_PERMISSION_EDIT) continue;

    // Role-based permission (e.g., Editor, Admin). Grafana 9+ returns
    // built-in role grants under "builtInRole"; older instances used "role".
    // The grant means "minimum required role", so the user's org role must
    // RANK AT OR ABOVE it (an Admin passes an Editor-only grant).
    const roleValue = ((item.builtInRole ?? item.role) as string | undefined)?.toLowerCase();
    if (
      roleValue &&
      orgRole &&
      (BUILT_IN_ROLE_RANK[orgRole.toLowerCase()] ?? -1) >= (BUILT_IN_ROLE_RANK[roleValue] ?? Infinity)
    ) {
      return true;
    }

    // Team-based permission.
    if (item.teamId && teamIds.has(item.teamId as number)) {
      return true;
    }

    // User-based permission.
    if (item.userLogin && item.userLogin === req.grafanaUsername) {
      return true;
    }
  }

  return false;
}

interface StandardField {
  name: string;
  location: "label" | "annotation";
  required: boolean;
  explanation: string;
  constraints: Record<string, unknown>;
  default_value: string | null;
}

export interface StandardSchema {
  version: string;
  fields: StandardField[];
}

let standardSchemaCache: StandardSchema | null = null;

async function getStandardSchema(): Promise<StandardSchema> {
  if (standardSchemaCache) return standardSchemaCache;
  const response = await complianceFetch("/standard/schema");
  if (!response.ok) {
    throw new Error(`Failed to load standard schema: ${response.status}`);
  }
  standardSchemaCache = (await response.json()) as StandardSchema;
  return standardSchemaCache;
}

function validateClonePayload(
  schema: StandardSchema,
  labels: Record<string, string>,
  annotations: Record<string, string>
): string[] {
  const missing: string[] = [];
  for (const field of schema.fields) {
    if (!field.required) continue;

    if (field.name === "Appchi-$operator") {
      const operatorKey = Object.keys(labels).find((key) => key.startsWith("Appchi - "));
      if (!operatorKey) {
        missing.push(field.name);
      } else if (labels[operatorKey]?.trim() !== "true") {
        missing.push(`${field.name} (label '${operatorKey}' must have the value 'true')`);
      }
      continue;
    }

    const raw = field.location === "label" ? labels[field.name] : annotations[field.name];
    if (raw === undefined || raw.trim() === "") {
      missing.push(field.name);
    }
  }
  return missing;
}

interface StandardFieldConstraints {
  exact_value?: string | null;
  starts_with?: string | null;
  allowed_values?: string[] | null;
  min_length?: number | null;
  max_length?: number | null;
}

// Case-insensitive key lookup, preferring an exact match (mirrors
// compliance engine's _get_case_insensitive in mapper.py).
function lookupCaseInsensitive(
  mapping: Record<string, string>,
  key: string
): string | undefined {
  if (key in mapping) return mapping[key];
  const keyLower = key.toLowerCase();
  for (const k of Object.keys(mapping)) {
    if (k.toLowerCase() === keyLower) return mapping[k];
  }
  return undefined;
}

// Mirrors resolve_field_value in compliance mapper.py so bulk classification
// matches per-rule /api/rules/load verdicts. Returns the observed raw value
// or undefined if the field is absent/empty.
function resolveFieldValue(
  field: StandardField,
  labels: Record<string, string>,
  annotations: Record<string, string>
): string | undefined {
  if (field.location === "label") {
    if (field.name === "Appchi-$operator") {
      return Object.keys(labels).find((key) => key.startsWith("Appchi - "));
    }
    if (field.name === "Send-Alert") {
      return labels["Send-Alert"];
    }
    if (field.name === "component") {
      // Python engine uses `get("component") or get("object")`: an
      // empty-string component falls through to the object label too.
      return labels["component"] || labels["object"];
    }
    return lookupCaseInsensitive(labels, field.name);
  }
  if (field.location === "annotation") {
    return annotations[field.name];
  }
  return undefined;
}

// Schema-based classification mirroring evaluate_rule in the compliance
// engine (exact_value, starts_with, allowed_values, min/max_length).
// Returns the failed field names; empty array means the rule is fully v2.
// All schema fields are checked (not just required ones), excluding the
// notification-policy membership check, which is per-user data the engine
// applies only in /api/rules/load.
export function classifyRuleAgainstSchema(
  schema: StandardSchema,
  labels: Record<string, string>,
  annotations: Record<string, string>
): string[] {
  const failed: string[] = [];
  for (const field of schema.fields) {
    const observed = resolveFieldValue(field, labels, annotations);
    // The Python engine treats only "" / None as missing (no trim) and runs
    // constraints against the raw value — mirror that so a whitespace-only
    // value is judged by its actual length.
    if (observed === undefined || observed === "") {
      if (field.required) failed.push(field.name);
      continue;
    }

    const constraints = (field.constraints ?? {}) as StandardFieldConstraints;

    if (constraints.exact_value !== undefined && constraints.exact_value !== null) {
      // Not a perfect mirror of the engine (which compares exactly): a rule
      // carrying "TRUE"/"True" fails /api/rules/load but here passes. That
      // skew is deliberate and bounded — the seeder only uses the result to
      // decide whether a "-v2" rule was TOOL-made, and Grafana's
      // provisioning API has surfaced this label as "TRUE"/"True"; those
      // hand-migrated equivalents must still seed-recognize.
      if (observed.toLowerCase() !== String(constraints.exact_value).toLowerCase()) {
        failed.push(field.name);
      }
      continue;
    }

    if (field.name === "Appchi-$operator") {
      if (!observed.startsWith(constraints.starts_with ?? "Appchi - ")) {
        failed.push(field.name);
      }
      continue;
    }

    if (
      constraints.min_length !== undefined &&
      constraints.min_length !== null &&
      observed.length < constraints.min_length
    ) {
      failed.push(field.name);
      continue;
    }
    if (
      constraints.max_length !== undefined &&
      constraints.max_length !== null &&
      observed.length > constraints.max_length
    ) {
      failed.push(field.name);
      continue;
    }

    if (
      constraints.allowed_values !== undefined &&
      constraints.allowed_values !== null &&
      !constraints.allowed_values.includes(observed)
    ) {
      failed.push(field.name);
      continue;
    }
  }
  return failed;
}

const loadSchema = z.object({
  rule_url: z.string().min(1),
});

const cloneSchema = z.object({
  labels: z.record(z.string()),
  annotations: z.record(z.string()),
  folder_url: z.string().min(1).optional(),
});

const folderUrlSchema = z.object({
  folder_url: z.string().min(1),
});

const createRuleSchema = z.object({
  folder_url: z.string().min(1),
  title: z.string().min(1),
  labels: z.record(z.string()),
  annotations: z.record(z.string()),
});

function parseFolderUid(url: string): string | null {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname.replace(/\/$/, "");

    const dashboardsMatch = path.match(/\/dashboards\/f\/([^/]+)/);
    if (dashboardsMatch) return dashboardsMatch[1];

    const foldersMatch = path.match(/\/folders\/([^/]+)/);
    if (foldersMatch) return foldersMatch[1];

    return null;
  } catch {
    return null;
  }
}

router.post("/load", async (req: AuthenticatedRequest, res) => {
  const parsed = loadSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "Bad Request", message: "rule_url is required." });
    return;
  }

  try {
    const response = await complianceFetch("/evaluate", {
      method: "POST",
      headers: sessionHeaders(req.grafanaUrl!, req.grafanaUsername!, req.grafanaPassword!),
      body: JSON.stringify({
        rule_url: parsed.data.rule_url,
        grafana_url: req.grafanaUrl,
        grafana_username: req.grafanaUsername,
        grafana_password: req.grafanaPassword,
      }),
    });
    const data = (await response.json()) as Record<string, unknown>;
    // Plan §7.3: single-rule load of an already-migrated rule. The form still
    // opens (reviewing is legitimate) — the frontend shows a warning banner.
    // Same copy-exists guard as the folder path (§3.2): a deleted v2 copy
    // means the original is migratable again and must not be flagged. Only
    // a 404 counts as "copy gone"; on any other error (auth/network) the
    // flag stays unset — a missing badge is the recoverable direction.
    if (response.ok && typeof data?.rule_uid === "string") {
      const previous = await latestMigrationForOriginal(data.rule_uid);
      if (previous?.created_uid) {
        const adminClient = adminGrafanaClient(req.grafanaUrl!);
        if (!adminClient) {
          console.warn("[load] no admin credentials — cannot check copy-exists for the migrated flag");
        } else {
          try {
            await adminClient.getRule(previous.created_uid);
            data.migrated = true;
          } catch (error) {
            if (!(error instanceof RuleNotFoundError)) {
              console.warn(`[load] copy-exists check for ${previous.created_uid} failed:`, error);
            }
            // 404 or check failure: leave migrated unset (§5.1 un-flag).
          }
        }
      }
    }
    res.status(response.status).json(data);
  } catch (error) {
    res.status(502).json({
      error: "Compliance Service Error",
      message: error instanceof Error ? error.message : String(error),
    });
  }
});

router.post("/load-folder", async (req: AuthenticatedRequest, res) => {
  const parsed = folderUrlSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "Bad Request", message: "folder_url is required." });
    return;
  }

  const folderUid = parseFolderUid(parsed.data.folder_url);
  if (!folderUid) {
    res.status(400).json({ error: "Bad Request", message: "Could not parse folder UID from URL." });
    return;
  }

  try {
    const client = new GrafanaClient({
      baseUrl: req.grafanaUrl!,
      username: req.grafanaUsername!,
      password: req.grafanaPassword!,
    });

    const rules = await client.listRulesInFolder(folderUid);
    const baseUrl = req.grafanaUrl!.replace(/\/$/, "");
    const items = rules.map((rule) => ({
      uid: rule.uid as string,
      title: (rule.title as string) || "Untitled rule",
      rule_url: `${baseUrl}/alerting/grafana/${rule.uid as string}/view`,
    }));

    res.json({ folder_uid: folderUid, rules: items });
  } catch (error) {
    console.error("[load-folder] error:", error);
    if (error instanceof GrafanaAuthError) {
      res.status(401).json({ error: "Unauthorized", message: error.message });
      return;
    }
    if (error instanceof GrafanaAPIError) {
      res.status(error.statusCode >= 500 ? 502 : 400).json({
        error: "Grafana API Error",
        message: error.message,
      });
      return;
    }
    res.status(502).json({
      error: "Folder load failed",
      message: error instanceof Error ? error.message : String(error),
    });
  }
});

interface ClassifiedRule {
  uid: string;
  title: string;
  rule_url: string;
  missing_fields?: string[];
  migrated?: boolean;
}

function classifyFolderRule(
  schema: StandardSchema,
  rule: Record<string, unknown>,
  baseUrl: string
): { summary: ClassifiedRule; failedFields: string[] } {
  const uid = rule.uid as string;
  const labels = (rule.labels as Record<string, string>) || {};
  const annotations = (rule.annotations as Record<string, string>) || {};
  const failedFields = classifyRuleAgainstSchema(schema, labels, annotations);
  return {
    failedFields,
    summary: {
      uid,
      title: (rule.title as string) || "Untitled rule",
      rule_url: `${baseUrl}/alerting/grafana/${uid}/view`,
      ...(failedFields.length > 0 ? { missing_fields: failedFields } : {}),
    },
  };
}

router.post("/load-folder-classified", async (req: AuthenticatedRequest, res) => {
  const parsed = folderUrlSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "Bad Request", message: "folder_url is required." });
    return;
  }

  const folderUid = parseFolderUid(parsed.data.folder_url);
  if (!folderUid) {
    res.status(400).json({ error: "Bad Request", message: "Could not parse folder UID from URL." });
    return;
  }

  try {
    const schema = await getStandardSchema();
    const client = new GrafanaClient({
      baseUrl: req.grafanaUrl!,
      username: req.grafanaUsername!,
      password: req.grafanaPassword!,
    });

    const rules = await client.listRulesInFolder(folderUid);
    const baseUrl = req.grafanaUrl!.replace(/\/$/, "");

    const v1: ClassifiedRule[] = [];
    const v2: ClassifiedRule[] = [];
    for (const rule of rules) {
      const { summary, failedFields } = classifyFolderRule(schema, rule, baseUrl);
      (failedFields.length === 0 ? v2 : v1).push(summary);
    }

    // One-time seeder pass on the first load of this folder (plan §6):
    // discovers pre-existing `X` + `X -v2` pairs and writes them into the
    // registry. Uses only the rules fetched above — no extra Grafana calls.
    try {
      await seedFolder(
        folderUid,
        rules.map((rule) => ({
          uid: rule.uid as string,
          title: (rule.title as string) || "Untitled rule",
          labels: (rule.labels as Record<string, string>) || {},
          annotations: (rule.annotations as Record<string, string>) || {},
        })),
        schema
      );
    } catch (seedErr) {
      // Registry failure must never break the folder load (plan §4.2).
      console.warn("[load-folder-classified] seeding failed:", seedErr);
    }

    // Migrated flag at read time (plan §3.1): registry row for the original
    // uid AND its created copy still among this folder's v2 rules (§3.2 —
    // deleting the v2 copy un-flags the original).
    const flags = await computeMigratedFlags(
      folderUid,
      new Set(v2.map((rule) => rule.uid))
    );
    for (const rule of v1) {
      if (flags.get(rule.uid)) rule.migrated = true;
    }
    // Not-migrated rules keep today's order; migrated rules sink to the
    // end of the v1 list (plan §7.1) — nothing is hidden.
    v1.sort((a, b) => Number(a.migrated ?? false) - Number(b.migrated ?? false));

    res.json({
      folder_uid: folderUid,
      counts: { v1: v1.length, v2: v2.length },
      v1,
      v2,
    });
  } catch (error) {
    console.error("[load-folder-classified] error:", error);
    if (error instanceof GrafanaAuthError) {
      res.status(401).json({ error: "Unauthorized", message: error.message });
      return;
    }
    if (error instanceof GrafanaAPIError) {
      res.status(error.statusCode >= 500 ? 502 : 400).json({
        error: "Grafana API Error",
        message: error.message,
      });
      return;
    }
    res.status(502).json({
      error: "Folder load failed",
      message: error instanceof Error ? error.message : String(error),
    });
  }
});

router.delete("/view/:uid", async (req: AuthenticatedRequest, res) => {
  try {
    const adminClient = adminGrafanaClient(req.grafanaUrl!);
    if (!adminClient) {
      res.status(500).json({ error: "Config Error", message: "Grafana admin credentials are not configured." });
      return;
    }

    console.log(`[view-delete] fetching rule ${req.params.uid}`);
    const rule = await adminClient.getRule(req.params.uid);
    const folderUid = (rule.folderUID as string) || "";
    if (!folderUid) {
      res.status(400).json({ error: "Bad Request", message: "Rule has no folder UID." });
      return;
    }

    const allowed = await checkFolderPermission(adminClient, folderUid, req);
    if (!allowed) {
      res.status(403).json({
        error: "Forbidden",
        message: "You do not have Edit/Admin permission for the folder this alert rule belongs to.",
      });
      return;
    }

    console.log(`[view-delete] deleting rule ${req.params.uid}`);
    await adminClient.deleteRule(req.params.uid);
    // Audit note only (plan §5.1): the copy-exists guard handles the
    // un-flagging — the original becomes migratable again automatically.
    void recordViewDelete(
      req.params.uid,
      (rule.title as string) || "",
      folderUid,
      req.grafanaUsername ?? null
    );
    res.json({ status: "ok", deleted: req.params.uid });
  } catch (error) {
    handleRuleError(error, res);
  }
});

// In-place edit of a v2 rule from the review view: validates the submitted
// labels/annotations against the standard schema (the rule must remain v2
// after the edit) and updates the rule in Grafana without cloning.
const viewUpdateSchema = z.object({
  title: z.string().min(1),
  labels: z.record(z.string()),
  annotations: z.record(z.string()),
  // Annotation keys the user emptied in the form; they are removed from the
  // rule instead of being kept by the merge below.
  clearAnnotations: z.array(z.string()).optional(),
  // Label keys to remove (e.g. the previous "Appchi - <operator>" key after the
  // operator was changed to a different one — a merge alone can't delete keys).
  clearLabels: z.array(z.string()).optional(),
});

router.put("/view/:uid", async (req: AuthenticatedRequest, res) => {
  const parsed = viewUpdateSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "Bad Request", message: "title, labels and annotations are required." });
    return;
  }

  try {
    const schema = await getStandardSchema();
    const failed = classifyRuleAgainstSchema(schema, parsed.data.labels, parsed.data.annotations);
    if (failed.length > 0) {
      res.status(400).json({
        error: "Validation Error",
        message: `This edit would take the rule out of v2. Failing fields: ${failed.join(", ")}`,
      });
      return;
    }

    const adminClient = adminGrafanaClient(req.grafanaUrl!);
    if (!adminClient) {
      res.status(500).json({ error: "Config Error", message: "Grafana admin credentials are not configured." });
      return;
    }

    console.log(`[view-update] fetching rule ${req.params.uid}`);
    const rule = await adminClient.getRule(req.params.uid);
    const folderUid = (rule.folderUID as string) || "";
    if (!folderUid) {
      res.status(400).json({ error: "Bad Request", message: "Rule has no folder UID." });
      return;
    }

    const allowed = await checkFolderPermission(adminClient, folderUid, req);
    if (!allowed) {
      res.status(403).json({
        error: "Forbidden",
        message: "You do not have Edit/Admin permission for the folder this alert rule belongs to.",
      });
      return;
    }

    // Merge the submitted values over the rule's existing labels/annotations so
    // non-schema keys (labels the rule carries that the standard doesn't know
    // about, e.g. team tags) survive the edit instead of being wiped. Keys the
    // user explicitly cleared are removed rather than merged back.
    const existingLabels = (rule.labels as Record<string, string>) || {};
    const existingAnnotations = (rule.annotations as Record<string, string>) || {};
    const clearAnnotationsSet = new Set(parsed.data.clearAnnotations || []);
    const clearLabelsSet = new Set(parsed.data.clearLabels || []);
    const mergedLabels: Record<string, string> = { ...existingLabels };
    for (const [key, value] of Object.entries(parsed.data.labels)) {
      if (value !== "") mergedLabels[key] = value;
    }
    for (const key of clearLabelsSet) delete mergedLabels[key];
    const mergedAnnotations: Record<string, string> = { ...existingAnnotations };
    for (const [key, value] of Object.entries(parsed.data.annotations)) {
      if (value !== "") mergedAnnotations[key] = value;
      else delete mergedAnnotations[key];
    }
    for (const key of clearAnnotationsSet) delete mergedAnnotations[key];

    // Update only what the form edits; keep the rest of the rule (condition,
    // queries, for-duration, group, folder) untouched.
    const updated = {
      ...rule,
      title: parsed.data.title,
      labels: mergedLabels,
      annotations: mergedAnnotations,
    };
    console.log(`[view-update] updating rule ${req.params.uid}`);
    const result = await adminClient.updateRule(req.params.uid, updated);
    const resultUid = (result.uid as string) || req.params.uid;
    res.json({
      status: "ok",
      updated: resultUid,
      title: parsed.data.title,
      rule_url: `${req.grafanaUrl}/alerting/grafana/${resultUid}/view`,
    });
  } catch (error) {
    handleRuleError(error, res);
  }
});

async function performClone(
  req: AuthenticatedRequest,
  labels: Record<string, string>,
  annotations: Record<string, string>,
  folderUrl?: string,
  // clone-and-delete: the registry row is written by the route handler AFTER
  // the original is actually deleted, so the row always reflects the final
  // state (plan §5: one row per user action).
  skipRegistryWrite = false
): Promise<{
  status: "ok";
  action: "created" | "updated";
  rule_uid: string;
  rule_url: string;
  title: string;
}> {
  const schema = await getStandardSchema();
  const missing = validateClonePayload(schema, labels, annotations);
  if (missing.length > 0) {
    throw { status: 400, message: `Missing required fields: ${missing.join(", ")}` };
  }

  const adminClient = adminGrafanaClient(req.grafanaUrl!);
  if (!adminClient) {
    throw { status: 500, message: "Grafana admin credentials are not configured." };
  }

  console.log(`[clone] fetching original rule ${req.params.uid}`);
  const original = await adminClient.getRule(req.params.uid);
  console.log(`[clone] fetched rule title: ${original.title}`);

  // Click-time registry re-check (plan §3.1): a rule migrated moments ago —
  // even in the same batch walk, or by another user in the same folder —
  // must not be cloned again. The frontend filters already; this is the
  // belt to that suspenders.
  const previous = await latestMigrationForOriginal(req.params.uid);
  if (previous?.created_uid) {
    // Idempotent re-click protection: only block when the copy still
    // exists in Grafana. If the user deleted the copy, re-migration is
    // the legitimate path (plan §3.2) and must go through. Only a 404
    // counts as "copy gone" — an auth/network error must not silently
    // unlock a duplicate, so it blocks (fail-closed) and logs.
    try {
      await adminClient.getRule(previous.created_uid);
      throw {
        status: 409,
        message: `This rule has already been migrated (copy: ${previous.created_title}). Delete the v2 copy first if you want to migrate it again.`,
      };
    } catch (error) {
      if (error instanceof RuleNotFoundError) {
        // Copy is gone from Grafana — allow the re-migration.
      } else if (typeof error === "object" && error !== null && "status" in error) {
        throw error;
      } else {
        console.warn(
          `[clone] copy-exists check for ${previous.created_uid} failed — blocking re-clone to stay safe:`,
          error
        );
        throw {
          status: 502,
          message: "Could not verify whether this rule's v2 copy still exists. Try again shortly.",
        };
      }
    }
  }

  const sourceFolderUid = (original.folderUID as string) || "";
  if (!sourceFolderUid) {
    throw { status: 400, message: "Original rule has no folder UID." };
  }

  const destFolderUid = folderUrl ? parseFolderUid(folderUrl) : sourceFolderUid;
  if (!destFolderUid) {
    throw { status: 400, message: "Could not parse folder UID from the destination folder URL." };
  }

  // Permission is required on the source folder only when no destination folder
  // is given (the clone lands in the same folder as the original). With a
  // destination folder, only Edit/Admin on that folder is required — the user
  // may not be able to see the source folder's permissions at all.
  let allowed: boolean;
  if (destFolderUid === sourceFolderUid) {
    allowed = await checkFolderPermission(adminClient, sourceFolderUid, req);
  } else {
    allowed = await checkFolderPermission(adminClient, destFolderUid, req);
  }
  if (!allowed) {
    const folderField = destFolderUid === sourceFolderUid ? "this alert rule belongs to" : "chosen as the destination";
    throw {
      status: 403,
      message: `You do not have Edit/Admin permission for the folder ${folderField}.`,
    };
  }

  const cloned = buildClonedRule(original, labels, annotations, destFolderUid);
  const newTitle = cloned.title as string;

  console.log(`[clone] checking for existing rule titled: ${newTitle}`);
  // The title search is scoped to the DESTINATION folder only. A global
  // title match would overwrite an unrelated same-titled rule in a folder
  // the caller may not even be able to read (admin creds bypass folder
  // permissions). In-destination collision stays an idempotent update;
  // there the caller already holds Edit/Admin by the check above.
  // Everything else leaves Grafana's own duplicate-title 409 to fire.
  const existing = (await adminClient.listRulesInFolder(destFolderUid)).find(
    (r) => r.title === newTitle
  );

  let result: Record<string, unknown>;
  let action: "created" | "updated";
  if (existing?.uid) {
    console.log(`[clone] updating existing rule ${existing.uid} in destination folder`);
    const ruleForUpdate = { ...cloned, uid: existing.uid };
    result = await adminClient.updateRule(existing.uid as string, ruleForUpdate);
    action = "updated";
  } else {
    console.log(`[clone] creating new rule with title: ${newTitle}`);
    result = await adminClient.createEditableRule(cloned);
    action = "created";
  }
  console.log(`[clone] ${action} rule response:`, result);

  const newUid = (result.uid as string) || (existing?.uid as string);
  const ruleUrl = newUid
    ? `${req.grafanaUrl}/alerting/grafana/${newUid}/view`
    : `${req.grafanaUrl}/alerting/list`;

  // Registry write after Grafana confirmed success, before answering the
  // frontend (plan §5). Fire-and-forget: a registry failure never fails the
  // clone (§4.2 — worst case the badge reappears via the seeder).
  // clone-and-delete skips this and writes its single row after the delete
  // so the row reflects the action's final state.
  if (newUid && !skipRegistryWrite) {
    void recordMigration({
      originalUid: (original.uid as string) || null,
      originalTitle: (original.title as string) || null,
      createdUid: newUid,
      createdTitle: newTitle,
      folderUid: destFolderUid,
      deletedOriginal: false,
      createdVia: "clone",
      migratedBy: req.grafanaUsername ?? null,
    });
  }

  return {
    status: "ok",
    action,
    rule_uid: newUid,
    rule_url: ruleUrl,
    title: newTitle,
  };
}

router.post("/:uid/clone", async (req: AuthenticatedRequest, res) => {
  const parsed = cloneSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "Bad Request", message: "labels and annotations are required." });
    return;
  }

  try {
    const result = await performClone(req, parsed.data.labels, parsed.data.annotations, parsed.data.folder_url);
    res.json(result);
  } catch (error) {
    handleRuleError(error, res);
  }
});

router.post("/:uid/clone-and-delete", async (req: AuthenticatedRequest, res) => {
  const parsed = cloneSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "Bad Request", message: "labels and annotations are required." });
    return;
  }

  try {
    // performClone skips the registry write; this handler writes the single
    // row for the whole action AFTER the original is deleted, so the row
    // records deleted_original: 1 with the correct folder (plan §5).
    const result = await performClone(
      req,
      parsed.data.labels,
      parsed.data.annotations,
      parsed.data.folder_url,
      true
    );
    const adminClient = adminGrafanaClient(req.grafanaUrl!);
    if (!adminClient) {
      res.status(500).json({ error: "Config Error", message: "Grafana admin credentials are not configured." });
      return;
    }
    // Deleting the original requires Edit/Admin permission on the folder the
    // original rule lives in — even when the clone went to a destination
    // folder (whose permission performClone already checked, but that does
    // not cover deleting the source rule).
    const original = await adminClient.getRule(req.params.uid);
    const sourceFolderUid = (original.folderUID as string) || "";
    if (!sourceFolderUid) {
      res.status(400).json({ error: "Bad Request", message: "Original rule has no folder UID." });
      return;
    }
    const sourceAllowed = await checkFolderPermission(adminClient, sourceFolderUid, req);
    if (!sourceAllowed) {
      res.status(403).json({
        error: "Forbidden",
        message: "You do not have Edit/Admin permission for the folder the original alert rule belongs to.",
      });
      return;
    }
    console.log(`[clone-and-delete] deleting original rule ${req.params.uid}`);
    await adminClient.deleteRule(req.params.uid);
    // Single registry row for the whole action (plan §5): the original is
    // gone, so deleted_original: 1. A previous migration row for this
    // original (if the copy was deleted and this is a re-migration) stays
    // as history.
    void recordMigration({
      originalUid: req.params.uid,
      originalTitle: (original.title as string) || null,
      createdUid: result.rule_uid,
      createdTitle: result.title,
      folderUid: sourceFolderUid,
      deletedOriginal: true,
      createdVia: "clone-and-delete",
      migratedBy: req.grafanaUsername ?? null,
    });
    res.json({ ...result, original_deleted: true });
  } catch (error) {
    handleRuleError(error, res);
  }
});

router.post("/create", async (req: AuthenticatedRequest, res) => {
  const parsed = createRuleSchema.safeParse(req.body);
  if (!parsed.success) {
    res.status(400).json({ error: "Bad Request", message: "folder_url, title, labels and annotations are required." });
    return;
  }

  console.log("[create] request:", JSON.stringify(parsed.data));

  try {
    const schema = await getStandardSchema();
    const missing = validateClonePayload(schema, parsed.data.labels, parsed.data.annotations);
    if (missing.length > 0) {
      res.status(400).json({
        error: "Validation Error",
        message: `Missing required fields: ${missing.join(", ")}`,
      });
      return;
    }

    const adminClient = adminGrafanaClient(req.grafanaUrl!);
    if (!adminClient) {
      res.status(500).json({ error: "Config Error", message: "Grafana admin credentials are not configured." });
      return;
    }

    const folderUid = parseFolderUid(parsed.data.folder_url);
    if (!folderUid) {
      res.status(400).json({ error: "Bad Request", message: "Could not parse folder UID from URL." });
      return;
    }

    console.log("[create] folderUid:", folderUid);
    const allowed = await checkFolderPermission(adminClient, folderUid, req);
    if (!allowed) {
      res.status(403).json({
        error: "Forbidden",
        message: "You do not have Edit/Admin permission for this folder.",
      });
      return;
    }

    const rule = buildSkeletonRule(folderUid, parsed.data.title, parsed.data.labels, parsed.data.annotations);
    console.log("[create] rule payload:", JSON.stringify(rule));
    const result = await adminClient.createEditableRule(rule);
    console.log("[create] grafana response:", JSON.stringify(result));
    const newUid = result.uid as string;
    const ruleUrl = newUid
      ? `${req.grafanaUrl}/alerting/grafana/${newUid}/view`
      : `${req.grafanaUrl}/alerting/list`;

    // Brand-new rule: no original, registry row for history (plan §5).
    if (newUid) {
      void recordMigration({
        originalUid: null,
        originalTitle: null,
        createdUid: newUid,
        createdTitle: parsed.data.title,
        folderUid,
        deletedOriginal: false,
        createdVia: "create",
        migratedBy: req.grafanaUsername ?? null,
      });
    }

    res.json({
      status: "ok",
      action: "created",
      rule_uid: newUid,
      rule_url: ruleUrl,
      title: parsed.data.title,
    });
  } catch (error) {
    handleRuleError(error, res);
  }
});

function handleRuleError(error: unknown, res: Response): void {
  console.error("[clone] error:", error);
  if (typeof error === "object" && error !== null && "status" in error) {
    const err = error as { status: number; message: string };
    res.status(err.status).json({ error: "Request failed", message: err.message });
    return;
  }
  if (error instanceof GrafanaAuthError) {
    res.status(401).json({ error: "Unauthorized", message: error.message });
    return;
  }
  if (error instanceof RuleNotFoundError) {
    res.status(404).json({ error: "Not Found", message: error.message });
    return;
  }
  if (error instanceof GrafanaAPIError) {
    const status = error.statusCode === 403 ? 403 : error.statusCode >= 500 ? 502 : 400;
    res.status(status).json({
      error: "Grafana API Error",
      message: error.message,
    });
    return;
  }
  res.status(502).json({
    error: "Clone failed",
    message: error instanceof Error ? error.message : String(error),
  });
}

function buildClonedRule(
  original: Record<string, unknown>,
  labels: Record<string, string>,
  annotations: Record<string, string>,
  folderUid?: string
): Record<string, unknown> {
  const originalTitle = (original.title as string) || "Alert rule";
  const newTitle = originalTitle.endsWith("-v2") ? `${originalTitle}-v2` : `${originalTitle}-v2`;

  // Use only the labels coming from the UI (standard fields).
  // Annotations are merged so any existing message/summary is preserved unless overridden.
  const mergedAnnotations = {
    ...((original.annotations as Record<string, string>) || {}),
    ...annotations,
  };

  // Remove identifiers so Grafana treats this as a new rule.
  const {
    uid: _uid,
    id: _id,
    updated: _updated,
    updatedBy: _updatedBy,
    provenance: _provenance,
    ...ruleWithoutIds
  } = original;

  return {
    ...ruleWithoutIds,
    // Override the destination folder when the caller picked one; otherwise the
    // original folderUID (kept in ruleWithoutIds) applies.
    ...(folderUid ? { folderUID: folderUid } : {}),
    title: newTitle,
    labels,
    annotations: mergedAnnotations,
  };
}

function buildSkeletonRule(
  folderUid: string,
  title: string,
  labels: Record<string, string>,
  annotations: Record<string, string>
): Record<string, unknown> {
  return {
    title,
    folderUID: folderUid,
    ruleGroup: "default",
    condition: "A",
    data: [
      {
        refId: "A",
        queryType: "",
        relativeTimeRange: { from: 300, to: 0 },
        datasourceUid: "__expr__",
        model: {
          type: "math",
          expression: "vector(0) == 1",
          refId: "A",
        },
      },
    ],
    noDataState: "NoData",
    execErrState: "Alerting",
    for: "0",
    labels,
    annotations,
  };
}

function extractCreatedUid(
  created: Record<string, unknown>,
  expectedTitle: string
): string | undefined {
  const rules = Array.isArray(created.rules)
    ? (created.rules as Array<Record<string, unknown>>)
    : undefined;
  if (!rules) {
    return (created.uid as string) || undefined;
  }
  const match = rules.find((r) => r.title === expectedTitle);
  return (match?.uid as string) || (rules[0]?.uid as string) || undefined;
}

export default router;
