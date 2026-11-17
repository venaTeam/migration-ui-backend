import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Unit tests for the migrations registry (PLAN-v1-migrated-flag.md §4, §6).
 *
 * REGISTRY_DATA_DIR must be set BEFORE src/config.ts is evaluated (it reads
 * the env var once at import time). vi.hoisted runs before the static
 * imports below resolve, so each test run gets a fresh temp dir instead of
 * touching the app's ./data/migrations.db.
 */
vi.hoisted(() => {
  // node: builtins are safe here — only the project's own modules are
  // still un-imported at hoist time.
  const fs = require("node:fs") as typeof import("node:fs");
  const path = require("node:path") as typeof import("node:path");
  const os = require("node:os") as typeof import("node:os");
  process.env.REGISTRY_DATA_DIR = fs.mkdtempSync(
    path.join(os.tmpdir(), "registry-test-"),
  );
});

import fs from "fs";

const flagsModule = await import("./flags.js");
const storeModule = await import("./store.js");
const { baseTitleOf, computeMigratedFlags, seedFolder } = flagsModule;
// Type-only import: does not affect module evaluation order.
type RegistryRule = import("./flags.js").RegistryRule;
const { closeRegistry, registryFilePath } = storeModule;

// The schema used here mirrors the real one's shape: one required label
// (component), one required annotation (message), one defaulted label
// (Send-Alert). "Compliant" = all present; hand-made "-v2" rules lack them.
const schema = {
  version: "0.1.0",
  fields: [
    { name: "Send-Alert", location: "label", required: true, explanation: "", constraints: { exact_value: "true" }, default_value: "true" },
    { name: "component", location: "label", required: true, explanation: "", constraints: { min_length: 1 }, default_value: null },
    { name: "message", location: "annotation", required: true, explanation: "", constraints: { min_length: 1 }, default_value: null },
  ],
} as never;

function rule(uid: string, title: string, compliant = true): RegistryRule {
  return compliant
    ? { uid, title, labels: { component: "cpu", "Send-Alert": "true" }, annotations: { message: "m" } }
    : { uid, title, labels: {}, annotations: {} };
}

afterAll(() => {
  closeRegistry();
  fs.rmSync(process.env.REGISTRY_DATA_DIR!, { recursive: true, force: true });
});

describe("baseTitleOf", () => {
  it("strips the -v2 suffix the tool actually produces (no space)", () => {
    // buildClonedRule appends "-v2" verbatim: "CPU High" -> "CPU High-v2".
    expect(baseTitleOf("CPU High-v2")).toBe("CPU High");
  });

  it("strips a hand-typed ' -v2' suffix with a space", () => {
    expect(baseTitleOf("CPU High -v2")).toBe("CPU High");
  });

  it("walks chains one hop at a time", () => {
    expect(baseTitleOf("CPU High-v2-v2")).toBe("CPU High-v2");
  });

  it("returns null for titles without the suffix", () => {
    expect(baseTitleOf("CPU High")).toBeNull();
  });

  it("degenerate: a rule titled exactly '-v2' has no base (no nonsense audit rows)", () => {
    expect(baseTitleOf("-v2")).toBe("");
  });
});

describe("seedFolder honesty table (plan §6.2)", () => {
  beforeEach(() => {
    closeRegistry();
    fs.rmSync(registryFilePath(), { force: true });
  });

  it("pair: original + compliant -v2 copy -> row with deleted_original 0", async () => {
    const { migrationsForFolder } = await import("./store.js");
    const rules = [rule("o1", "CPU High", false), rule("c1", "CPU High-v2")];
    const n = await seedFolder("f", rules, schema);
    expect(n).toBe(1);
    const rows = await migrationsForFolder("f");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ original_uid: "o1", created_uid: "c1", deleted_original: 0 });
  });

  it("pair with hand-typed ' -v2' title is also matched", async () => {
    const { migrationsForFolder } = await import("./store.js");
    const rules = [rule("o1", "CPU High", false), rule("c1", "CPU High -v2")];
    await seedFolder("f", rules, schema);
    const rows = await migrationsForFolder("f");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ original_uid: "o1", created_uid: "c1" });
  });

  it("lone compliant -v2 (original deleted) -> row with deleted_original 1", async () => {
    const { migrationsForFolder } = await import("./store.js");
    const rules = [rule("lone", "CPU High-v2")];
    const n = await seedFolder("f", rules, schema);
    expect(n).toBe(1);
    const rows = await migrationsForFolder("f");
    expect(rows[0]).toMatchObject({ original_uid: null, created_uid: "lone", deleted_original: 1 });
  });

  it("chain: two hops -> one row per hop", async () => {
    const { migrationsForFolder } = await import("./store.js");
    const rules = [
      rule("o1", "CPU High", false),
      rule("c1", "CPU High-v2"),
      rule("c2", "CPU High-v2-v2"),
    ];
    const n = await seedFolder("f", rules, schema);
    expect(n).toBe(2);
    const rows = await migrationsForFolder("f");
    expect(rows.map((r) => [r.original_uid, r.created_uid]).sort()).toEqual([
      ["c1", "c2"],
      ["o1", "c1"],
    ]);
  });

  it("hand-made '-v2' failing the standard -> nothing written", async () => {
    const { migrationsForFolder } = await import("./store.js");
    const rules = [rule("hand", "foo-v2", false)];
    const n = await seedFolder("f", rules, schema);
    expect(n).toBe(0);
    expect(await migrationsForFolder("f")).toHaveLength(0);
  });

  it("lone base rule without any -v2 sibling -> nothing written", async () => {
    const { migrationsForFolder } = await import("./store.js");
    const rules = [rule("o1", "CPU High", false)];
    const n = await seedFolder("f", rules, schema);
    expect(n).toBe(0);
    expect(await migrationsForFolder("f")).toHaveLength(0);
  });

  it("runs exactly once per folder (plan §6.3)", async () => {
    const rules = [rule("o1", "CPU High", false), rule("c1", "CPU High-v2")];
    expect(await seedFolder("f", rules, schema)).toBe(1);
    expect(await seedFolder("f", rules, schema)).toBe(0);
    // ...and the one-time mark survives a "restart".
    closeRegistry();
    expect(await seedFolder("f", rules, schema)).toBe(0);
  });
});

describe("computeMigratedFlags (plan §3.2)", () => {
  beforeEach(() => {
    closeRegistry();
    fs.rmSync(registryFilePath(), { force: true });
  });

  it("flags the original only while its created copy still exists among v2", async () => {
    const { recordMigration } = await import("./store.js");
    await recordMigration({
      originalUid: "o1",
      originalTitle: "CPU High",
      createdUid: "c1",
      createdTitle: "CPU High-v2",
      folderUid: "f",
      createdVia: "clone",
    });
    expect((await computeMigratedFlags("f", new Set(["c1"]))).get("o1")).toBe(true);
    // Copy deleted -> un-flag (the legitimate re-migration path, §5.1).
    expect((await computeMigratedFlags("f", new Set())).get("o1")).toBe(false);
  });

  it("never flags created-only rows (original_uid null)", async () => {
    const { recordMigration } = await import("./store.js");
    await recordMigration({
      originalUid: null,
      createdUid: "c1",
      createdTitle: "New rule",
      folderUid: "f",
      createdVia: "create",
    });
    const flags = await computeMigratedFlags("f", new Set(["c1"]));
    expect(flags.size).toBe(0);
  });
});
