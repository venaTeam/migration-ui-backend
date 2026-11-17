import type { MigrationRow } from "./store.js";
import {
  isFolderSeeded,
  markFolderSeeded,
  migrationsForFolder,
  recordMigration,
} from "./store.js";
import { classifyRuleAgainstSchema, type StandardSchema } from "../routes/rules.js";

/**
 * Migrated-flag computation and the one-time seeder
 * (PLAN-v1-migrated-flag.md §3, §6).
 *
 * migrated = registry row exists for the original uid AND the created copy
 * from that row still exists among the folder's v2 rules (copy-exists
 * guard, §3.2). Computed at every folder load — never frozen.
 */

export interface RegistryRule {
  uid: string;
  title: string;
  labels: Record<string, string>;
  annotations: Record<string, string>;
}

export interface SeederPair {
  originalUid: string;
  originalTitle: string;
  createdUid: string;
  createdTitle: string;
}

/**
 * Base title of a clone candidate. Accepts both "-v2" suffix shapes the
 * tool and users produce: "CPU High-v2" (buildClonedRule appends "-v2"
 * verbatim) and "CPU High -v2" (hand-typed with a space). "CPU High" ->
 * null; "CPU High-v2-v2" -> "CPU High-v2" (one hop per call).
 */
export function baseTitleOf(title: string): string | null {
  const match = /^(.*?)-v2$/.exec(title);
  return match ? match[1].replace(/ $/, "") : null;
}

/**
 * One seeder pass over a folder's raw rules (plan §6.2). Both honesty
 * signals required: the title shape (base + " -v2") and schema compliance
 * of the candidate clone. Writes one registry row per discovered hop;
 * `-v2 -v2` chains produce one row per hop.
 * Returns the number of rows written.
 */
// Two concurrent first loads of one folder must not both run the seeding
// pass (each recordMigration is a full export+rename write; interleaved
// passes duplicate rows and can lose one writer's rows). Serialize the
// whole pass per folder in-process; a queued caller re-checks
// isFolderSeeded after the first pass finishes (the first caller's return
// value flows through the lock promise).
const folderSeedLocks = new Map<string, Promise<number>>();

export async function seedFolder(
  folderUid: string,
  rules: RegistryRule[],
  schema: StandardSchema
): Promise<number> {
  const pending = folderSeedLocks.get(folderUid);
  if (pending) {
    // Another pass is seeding this folder: wait for it, then make our
    // own decision (it probably seeded the folder — then we return 0).
    await pending.catch(() => {});
    if (await isFolderSeeded(folderUid)) return 0;
  }
  return seedFolderWithLock(folderUid, rules, schema);
}

function seedFolderWithLock(
  folderUid: string,
  rules: RegistryRule[],
  schema: StandardSchema
): Promise<number> {
  let releaseLock!: (value: number) => void;
  const lock = new Promise<number>((resolve) => {
    releaseLock = resolve;
  });
  folderSeedLocks.set(folderUid, lock);
  return seedFolderUnlocked(folderUid, rules, schema).then(
    (count) => {
      releaseLock(count);
      folderSeedLocks.delete(folderUid);
      return count;
    },
    (err) => {
      releaseLock(0);
      folderSeedLocks.delete(folderUid);
      throw err;
    }
  );
}

async function seedFolderUnlocked(
  folderUid: string,
  rules: RegistryRule[],
  schema: StandardSchema
): Promise<number> {
  if (await isFolderSeeded(folderUid)) {
    console.log(`[registry] folder ${folderUid} already seeded — skipping scan`);
    return 0;
  }

  const byTitle = new Map<string, RegistryRule>();
  for (const rule of rules) byTitle.set(rule.title, rule);

  // One row per (original, created) hop, ever: dedupe across rules and
  // against anything the registry already holds.
  const writtenPairs = new Set<string>();

  let pairsFound = 0;
  const record = async (input: Parameters<typeof recordMigration>[0]): Promise<void> => {
    const key = `${input.originalUid ?? ""}\u0000${input.createdUid}`;
    if (writtenPairs.has(key)) return;
    writtenPairs.add(key);
    await recordMigration(input);
    pairsFound += 1;
  };
  for (const row of await migrationsForFolder(folderUid)) {
    writtenPairs.add(`${row.original_uid ?? ""}\u0000${row.created_uid}`);
  }

  for (const rule of rules) {
    const base = baseTitleOf(rule.title);
    // "" would be a rule titled exactly "-v2" — no real original title
    // behind it, so no evidence (§6.2): skip rather than write a nonsense
    // audit row.
    if (base == null || base === "") continue;

    // Hand-made "-v2" names that fail the standard are NOT tool-made —
    // no evidence, no write (§6.2 honesty rules).
    const failed = classifyRuleAgainstSchema(schema, rule.labels, rule.annotations);
    if (failed.length > 0) continue;

    // Chain walk: "CPU High -v2 -v2" groups under base "CPU High -v2";
    // walk down one hop at a time, then handle the lone-clone case for the
    // deepest clone whose base title has no rule of its own.
    let created: RegistryRule = rule;
    let parent: RegistryRule | null = byTitle.get(base) ?? null;
    while (parent) {
      await record({
        originalUid: parent.uid,
        originalTitle: parent.title,
        createdUid: created.uid,
        createdTitle: created.title,
        folderUid,
        deletedOriginal: false,
        createdVia: "seed",
      });
      created = parent;
      const nextBase = baseTitleOf(parent.title);
      parent = nextBase != null ? (byTitle.get(nextBase) ?? null) : null;
    }

    // Lone clone (original deleted before this first scan): one row with
    // deleted_original = 1 — only for the deepest clone with no parent
    // rule, and only if the walk above didn't already pair it (§6.2).
    if (created === rule) {
      await record({
        originalUid: null,
        originalTitle: base,
        createdUid: rule.uid,
        createdTitle: rule.title,
        folderUid,
        deletedOriginal: true,
        createdVia: "seed",
      });
    }
  }

  await markFolderSeeded(folderUid, pairsFound);
  console.log(`[registry] seeded folder ${folderUid}: ${pairsFound} pair(s) written`);
  return pairsFound;
}

/**
 * The migrated flag per original-uid, computed at read time (plan §3.1):
 * a registry row for the uid AND the row's created copy still present
 * among the folder's v2 uids.
 */
export async function computeMigratedFlags(
  folderUid: string,
  v2Uids: Set<string>
): Promise<Map<string, boolean>> {
  const flags = new Map<string, boolean>();
  const rows = await migrationsForFolder(folderUid);
  for (const row of rows) {
    if (!row.original_uid) continue;
    const copyExists = v2Uids.has(row.created_uid);
    const prev = flags.get(row.original_uid);
    flags.set(row.original_uid, (prev ?? false) || copyExists);
  }
  return flags;
}

export type { MigrationRow };
