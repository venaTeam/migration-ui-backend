import fs from "fs";
import path from "path";
import { config } from "../config.js";

/**
 * The migrations registry (PLAN-v1-migrated-flag.md §4): one SQLite file,
 * two tables:
 *
 *   migrations — one row per migration this tool performed or discovered
 *   seed_scan  — folders whose one-time seeder pass is complete
 *
 * Storage engine is sql.js (SQLite compiled to WebAssembly): no native
 * module to build, works on any Node version, and reads/writes the regular
 * SQLite file format. Durability model: every write re-exports the whole
 * database to disk and replaces the file atomically (tmp file + rename).
 * At this scale (one row per migration) that is microseconds.
 *
 * Safety rule (plan §4.2): if the registry ever fails, the migration itself
 * must still succeed — every method catches its own errors and degrades to
 * "no memory" (log + continue). Worst case a migrated rule loses its badge
 * once; the seeder re-detects it on the next folder load.
 */

export interface MigrationRow {
  id: number;
  original_uid: string | null;
  original_title: string | null;
  created_uid: string;
  created_title: string;
  folder_uid: string | null;
  deleted_original: number;
  created_via: string;
  migrated_by: string | null;
  migrated_at: string;
}

const SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS migrations (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  original_uid     TEXT,
  original_title   TEXT,
  created_uid      TEXT NOT NULL,
  created_title    TEXT NOT NULL,
  folder_uid       TEXT,
  deleted_original INTEGER NOT NULL DEFAULT 0,
  created_via      TEXT NOT NULL,
  migrated_by      TEXT,
  migrated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_migrations_original ON migrations(original_uid);
CREATE INDEX IF NOT EXISTS idx_migrations_created  ON migrations(created_uid);
CREATE INDEX IF NOT EXISTS idx_migrations_folder   ON migrations(folder_uid);

CREATE TABLE IF NOT EXISTS seed_scan (
  folder_uid  TEXT PRIMARY KEY,
  seeded_at   TEXT NOT NULL DEFAULT (datetime('now')),
  pairs_found INTEGER NOT NULL DEFAULT 0
);
`;

type SqlJsDatabase = {
  run(sql: string, values?: unknown[]): unknown;
  prepare(sql: string): {
    bind(values?: unknown[]): void;
    step(): boolean;
    getAsObject(): Record<string, unknown>;
    free(): void;
  };
  export(): Uint8Array;
  close(): void;
};

let db: SqlJsDatabase | null = null;
let dbPath: string | null = null;
let initPromise: Promise<void> | null = null;

export function registryFilePath(): string {
  return path.join(config.registryDataDir, "migrations.db");
}

async function getDb(): Promise<SqlJsDatabase> {
  if (db) return db;
  if (!initPromise) {
    initPromise = (async () => {
      // ESM import of the CJS sql.js module; typed loosely, verified by use.
      const mod = (await import("sql.js")) as unknown as {
        default: (cfg?: Record<string, unknown>) => Promise<{
          Database: new (data?: Uint8Array) => SqlJsDatabase;
        }>;
      };
      // dist/sql-wasm.js needs to locate its .wasm file inside the package.
      const wasmDir = path.join(process.cwd(), "node_modules", "sql.js", "dist");
      const SQL = await mod.default({ locateFile: (file: string) => path.join(wasmDir, file) });
      const file = registryFilePath();
      fs.mkdirSync(path.dirname(file), { recursive: true });
      const database = fs.existsSync(file)
        ? new SQL.Database(new Uint8Array(fs.readFileSync(file)))
        : new SQL.Database();
      database.run(SCHEMA_SQL);
      const tmp = `${file}.tmp`;
      fs.writeFileSync(tmp, Buffer.from(database.export()));
      fs.renameSync(tmp, file);
      db = database;
      dbPath = file;
    })().catch((err) => {
      initPromise = null; // allow a retry on the next call
      throw err;
    });
  }
  await initPromise;
  return db as unknown as SqlJsDatabase;
}

/** Run a write through the database and persist to disk. */
async function write(
  fn: (database: SqlJsDatabase) => void,
  what: string
): Promise<void> {
  try {
    const database = await getDb();
    fn(database);
    if (!dbPath) return;
    const tmp = `${dbPath}.tmp`;
    fs.writeFileSync(tmp, Buffer.from(database.export()));
    fs.renameSync(tmp, dbPath);
  } catch (err) {
    console.warn(`[registry] write failed (${what}) — migration result unaffected:`, err);
  }
}

function selectAll(
  database: SqlJsDatabase,
  sql: string,
  values: unknown[] = []
): Record<string, unknown>[] {
  const stmt = database.prepare(sql);
  try {
    stmt.bind(values);
    const rows: Record<string, unknown>[] = [];
    while (stmt.step()) rows.push(stmt.getAsObject());
    return rows;
  } finally {
    stmt.free();
  }
}

export interface RecordMigrationInput {
  originalUid?: string | null;
  originalTitle?: string | null;
  createdUid: string;
  createdTitle: string;
  folderUid?: string | null;
  deletedOriginal?: boolean;
  createdVia: "clone" | "clone-and-delete" | "create" | "seed";
  migratedBy?: string | null;
}

/**
 * Write one migration row. Awaits only where the caller needs
 * read-after-write consistency (the seeder); route handlers call it
 * fire-and-forget (`void recordMigration(...)`).
 */
export async function recordMigration(input: RecordMigrationInput): Promise<void> {
  await write(
    (database) =>
      database.run(
        `INSERT INTO migrations
           (original_uid, original_title, created_uid, created_title,
            folder_uid, deleted_original, created_via, migrated_by, migrated_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))`,
        [
          input.originalUid ?? null,
          input.originalTitle ?? null,
          input.createdUid,
          input.createdTitle,
          input.folderUid ?? null,
          input.deletedOriginal ? 1 : 0,
          input.createdVia,
          input.migratedBy ?? null,
        ]
      ),
    `recordMigration ${input.originalUid ?? "new"} -> ${input.createdUid}`
  );
}

/** Audit-only row when a v2 copy is deleted from review (plan §5.1). */
export async function recordViewDelete(
  uid: string,
  title: string,
  folderUid: string | null,
  deletedBy: string | null
): Promise<void> {
  await write(
    (database) =>
      database.run(
        `INSERT INTO migrations
           (original_uid, original_title, created_uid, created_title,
            folder_uid, deleted_original, created_via, migrated_by, migrated_at)
         VALUES (NULL, NULL, ?, ?, ?, 1, 'view-delete', ?, datetime('now'))`,
        [uid, title, folderUid, deletedBy]
      ),
    `recordViewDelete ${uid}`
  );
}

/**
 * True if a seeder pass already ran to completion for this folder —
 * plan §6.3: one scan per folder, ever (delete the row to re-arm).
 */
export async function isFolderSeeded(folderUid: string): Promise<boolean> {
  try {
    const database = await getDb();
    return (
      selectAll(database, "SELECT 1 FROM seed_scan WHERE folder_uid = ?", [folderUid])
        .length > 0
    );
  } catch (err) {
    console.warn("[registry] seed_scan lookup failed — will rescan:", err);
    return false;
  }
}

/** Mark a folder's one-time seeder pass complete. Never throws. */
export async function markFolderSeeded(folderUid: string, pairsFound: number): Promise<void> {
  await write(
    (database) =>
      database.run(
        `INSERT INTO seed_scan (folder_uid, seeded_at, pairs_found)
         VALUES (?, datetime('now'), ?)
         ON CONFLICT(folder_uid) DO UPDATE SET seeded_at = datetime('now'), pairs_found = ?`,
        [folderUid, pairsFound, pairsFound]
      ),
    `markFolderSeeded ${folderUid}`
  );
}

/** All migration rows for one folder (for load-time flag computation). */
export async function migrationsForFolder(folderUid: string): Promise<MigrationRow[]> {
  try {
    const database = await getDb();
    return selectAll(
      database,
      `SELECT * FROM migrations WHERE folder_uid = ? ORDER BY id`,
      [folderUid]
    ) as unknown as MigrationRow[];
  } catch (err) {
    console.warn("[registry] migrations lookup failed — no migrated flags this load:", err);
    return [];
  }
}

/** Latest migration row for one original rule uid (click-time batch re-check). */
export async function latestMigrationForOriginal(originalUid: string): Promise<MigrationRow | null> {
  try {
    const database = await getDb();
    const rows = selectAll(
      database,
      `SELECT * FROM migrations WHERE original_uid = ? ORDER BY id DESC LIMIT 1`,
      [originalUid]
    ) as unknown as MigrationRow[];
    return rows[0] ?? null;
  } catch (err) {
    console.warn("[registry] original lookup failed:", err);
    return null;
  }
}

/** Test/ops helper: wipe in-memory state (next access re-opens the file). */
export function closeRegistry(): void {
  if (db) {
    try {
      db.close();
    } catch {
      // ignore
    }
  }
  db = null;
  dbPath = null;
  initPromise = null;
}
