# Plan: Migrated-Flag on v1 Rules (registry + overview badge + batch boundary)

> **Supersedes** `PLAN-hide-already-migrated-rules.md` (kept in git history). Same registry, same write points, same seeder — the behavior changes from *hiding* migrated v1 rules to *marking* them and gating the batch.

## 1. What is the problem?

The tool migrates a Grafana alert rule by making a **copy** of it, fixed up to meet the company alert standard. Every copy gets `-v2` added to its title:

```
Before:   "CPU High"          (old rule, doesn't meet the standard)
After:    "CPU High -v2"      (new rule this tool created, meets the standard)
```

When migrating, the user picks one of two actions:

| User picks | What happens in Grafana |
|---|---|
| **Clone** | Creates `CPU High -v2`. The original `"CPU High"` **stays**. |
| **Clone and delete** | Creates `CPU High -v2`. The original is removed. |

### The bug

The tool **forgets** what it migrated. Nothing is saved anywhere. So when the user opens the same folder again tomorrow:

- The old **original** (`CPU High`) is still listed as something to migrate — even though it was already migrated. Migrating it again creates a **second identical** `CPU High -v2` — a duplicate v2 rule in Grafana, duplicating notifications, next to the original copy.
- The user gets no signal, in the folder overview or in the wizard, that this rule is already done.

**What the user wants:** the v1 list should *show* which rules this tool already migrated — visibly, with a label — and the batch "Create all rules" should stop offering to re-create them. Not hidden: present, marked, and placed at the end of the v1 list.

### What the v1/v2 split (already merged) fixed — and what remains

The split feature sorts folder rules by schema compliance. Tool-created copies always meet the standard, so they land in the **v2 list** — the wizard never sees them, so `foo-v2-v2` chains can't be produced by the batch. That window is closed.

What remains is purely about **v1 rules**: an *original* that was left behind by a clone (without delete) fails the standard — that's exactly why it was migrated — so it lands back in the v1 list indistinguishable from an unmigrated rule.

> **The remaining hole in one sentence: a migrated original looks exactly like an unmigrated rule, because "it fails the standard" is true for both.**

---

## 2. The solution in one paragraph

The backend keeps a small **SQLite database file** — the **registry**. Every time the tool migrates or creates a rule, it **writes one row** into the registry: "I changed rule X into rule X-v2, at this time, for this user." When a folder is loaded, the backend does **not hide** anything: it attaches a `migrated: true|false` flag to every v1 rule. The frontend puts flagged rules **at the end** of the v1 list with a **"Migrated"** badge, shows them in the wizard as **notice cards with no form to fill**, and offers **"Create all rules" only up to the last not-migrated rule** — from the first migrated rule onward there is no batch option at all. Single re-migration stays possible (warn, but allow).

For rules migrated *before* this feature exists, a one-time **seeder** scans a folder for pairs like `CPU High` + `CPU High -v2` and writes them into the registry too — so even old migrations get remembered. After its first pass on a folder the seeder stops running (see §6).

---

## 3. How migration will work after this plan

```
User migrates "CPU High" (clone)
         │
         ▼
Tool creates "CPU High -v2" in Grafana          (unchanged, already works)
         │
         ▼
Tool writes a row in the registry:              ★ NEW
   original_uid:  abc123   ("CPU High")
   created_uid:   def456   ("CPU High -v2")
   deleted_original: 0 (kept) | 1 (deleted)
   migrated_by:   username
   migrated_at:   timestamp
         │
         ▼
Next folder load:
   "CPU High" is in the registry as migrated
        → v1 list shows it LAST, with a "Migrated" badge    ★ NEW
        → in the wizard: notice card, no fields to fill     ★ NEW
        → "Create all rules" is not offered on/after it     ★ NEW
   "CPU High -v2" → shows on the overview v2 side (unchanged)
```

### 3.1 The migrated flag is computed at read time, not frozen

"Migrated" is a moving target *within* a session: rules migrated earlier in the same walk are also in the registry by the time "Create all rules" is clicked. Two consequences:

- **The backend recomputes the flag at every folder load** (registry lookup — microseconds; no staleness between sessions).
- **The batch endpoint re-checks the registry at click time** before cloning each rule, so a create performed 20 seconds earlier in the same session is never duplicated. The frontend also only sends not-migrated rules in the batch payload — clicked-time check is the belt to that suspenders (protects against two users in the same folder too).

### 3.2 The copy-exists guard (migrated ≠ stranded)

`migrated: true` means BOTH:

1. A registry row exists for this original uid, AND
2. the `created_uid` from that row **still exists among the folder's v2 rules** (the classification fetch already lists them — a set-intersection, no extra Grafana call).

If the user later **deleted the v2 copy**, the original loses its flag: it becomes an ordinary v1 row again (no badge, batchable, wizard opens its form normally). That is the legitimate re-migration path — deleting the copy makes re-migration correct, and this handles the plan's old open "delete-case" decision without any manual registry editing.

### 3.3 Single Edit on a migrated rule: warn, but allow

Clicking **Edit** on a migrated row (or reaching it in the wizard) opens a **warning card** — *"This rule has already been migrated by this tool; migrating again creates a duplicate"* — and still offers the normal migration form. An intentional individual re-clone is a deliberate act and stays possible. The batch is the accident-prone path, and that is where the option disappears (§7). Any such re-migration writes its own registry row like any other (§5) — nothing special needed.

---

## 4. The registry (the database)

### 4.1 What it looks like

One SQLite file (e.g. `data/migrations.db`). One table of migrations, one bookkeeping table for the seeder:

```sql
-- One row per migration this tool performed (or discovered).
CREATE TABLE migrations (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  original_uid      TEXT,             -- the source rule's Grafana uid ("CPU High")
  original_title    TEXT,             -- "CPU High"      (for history/display)
  created_uid       TEXT NOT NULL,    -- the new rule's uid ("CPU High -v2")
  created_title     TEXT NOT NULL,
  folder_uid        TEXT,             -- which folder this happened in
  deleted_original  INTEGER NOT NULL DEFAULT 0,  -- 1 = clone-and-delete, 0 = kept
  created_via       TEXT NOT NULL,    -- 'clone' | 'clone-and-delete' | 'create' | 'seed'
  migrated_by       TEXT,             -- which user did it (from the session)
  migrated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Remembers folders whose history scan is complete (so the scan runs once).
CREATE TABLE seed_scan (
  folder_uid  TEXT PRIMARY KEY,
  seeded_at   TEXT NOT NULL DEFAULT (datetime('now')),
  pairs_found INTEGER NOT NULL DEFAULT 0
);
```

### 4.2 Where it lives

| Environment | Storage | What's added |
|---|---|---|
| Local (docker compose) | bind-mounted folder, e.g. `./data/ui-backend-registry:/data` | one volume line |
| OpenShift (vena) | **PVC** attached to the ui-backend pod | one PVC claim + mount |

New env var: `REGISTRY_DATA_DIR` (default `./data`). The registry file must be on a **persistent volume** — if the pod restarts with no volume, the tool forgets everything again (recoverable, but avoid).

Safety rule: if the registry ever fails (disk hiccup), **the migration still succeeds** — we only log a warning. Worst case: a migrated rule loses its badge once, and the seeder re-detects it on the next folder load.

### 4.3 Why SQLite

- A single file, no server, no extra container.
- Handles the scale trivially (one row per migration — thousands of rows is nothing for SQLite).
- Built-in transactions for bulk "Create all rules".
- Free audit history: "who migrated what, when" is one SELECT.

---

## 5. Every place the registry gets written

Each write happens **after** Grafana confirms success, **before** we answer the frontend:

| User action | Registry row |
|---|---|
| **Clone** (`POST /api/rules/:uid/clone`) | original kept → `deleted_original: 0` |
| **Clone and delete** (`POST /api/rules/:uid/clone-and-delete`) | original deleted → `deleted_original: 1` |
| **Create new v2 rule** (`POST /api/rules/create`) | `original_uid: NULL` (brand-new rule) |
| **"Create all rules"** (batch, individual or per-rule creates during the walk) | one row per created rule |
| **Delete a v2 rule from review** (`DELETE /api/rules/view/:uid`) | audit note only (see §5.1) |

### 5.1 Delete-case (resolved by the copy-exists guard)

If a user deletes `CPU High -v2` from the overview v2 side, its original `CPU High` **automatically becomes migratable again**: the flag computation (§3.2) no longer finds the created copy among the folder's v2 rules, so no badge is shown and the batch includes the original. No user decision to confirm, no registry surgery. (A `deleted_original: 0` row stays in the table purely as history.)

---

## 6. The seeder — remembering old migrations

### 6.1 Why it exists

The registry only knows about migrations done **after** it's installed. Folders already contain old `CPU High` + `CPU High -v2` pairs from before. Without a backfill, those old pairs keep appearing unflagged. The seeder fixes exactly that.

### 6.2 How it decides "this pair is a migration"

When a folder is loaded, the seeder looks at the rules that were fetched anyway (**no extra Grafana calls**) and requires **both** signals before writing a row:

1. **Title shape** — the clone's title is the base title + ` -v2` (`CPU High -v2 -v2` groups under base `CPU High`).
2. **Schema check** — the candidate clone is compliant: it has all the standard fields. This re-uses the v1/v2 split's classifier (`classifyRuleAgainstSchema`) — no new logic, no new code path.

| What's in the folder | Conclusion | Registry write |
|---|---|---|
| `CPU High` (v1) **and** `CPU High -v2` (v2) | migrated, original kept | row with `deleted_original: 0` |
| only `CPU High -v2` (v2), no original | migrated, original deleted later | row with `deleted_original: 1` |
| `CPU High -v2 -v2` (chain) | two migrations happened | one row per hop |
| `foo -v2` that **fails** the standard | a hand-made rule with a "-v2" name, NOT tool-made | **nothing written** — correctly stays unflagged in v1 |
| `CPU High` alone (no -v2 sibling) | no migration evidence | **nothing written** — stays unflagged in v1 |

That last two rows are the seeder's honesty rules: no evidence → no write.

### 6.3 When the seeder stops running

Every folder gets a **one-time** scan. The first folder load where the scan finds nothing new marks the folder in `seed_scan`. From then on, loading that folder skips the scan entirely — one tiny database check instead. So:

- seeding cost: **exactly once per folder, ever**
- steady-state cost per folder load: one indexed read (microseconds)
- to re-arm (e.g. a folder was restored from backup): delete that folder's row in `seed_scan`.

### 6.4 Known small gaps (accepted)

- A clone **moved to another folder** before its first seeding → its original keeps showing unflagged. Fix by hand: one manual registry insert, or rename the clone back to `-v2` once.
- A clone that was **renamed** before first seeding (e.g. to `CPU High fixed`) → invisible to the seeder. Same fix. We'll send one message at go-live: "if you renamed migrated rules, put their `-v2` name back once before the upgrade."

---

## 7. What the user sees

### 7.1 Folder overview — the 4 surface changes (frontend)

All on the **folder overview page** (the two-sided page that appears after loading a folder):

1. **Reordered v1 list**: not-migrated rules first (same order as today), migrated rules **at the end**, each carrying a **"Migrated"** badge.
2. **Per-row actions unchanged:** Open in Grafana / Edit / Delete work the same on migrated and not-migrated rows. Edit on a migrated row opens the warning card + form (§3.3).
3. **Empty state**: if *every* v1 rule is flagged, the v1 side shows *"All v1 rules in this folder have already been migrated."* (plus the badge rows below it — nothing is hidden); if the folder has no v1 rules at all, the existing empty line stays.
4. **v2 side: unchanged** — it keeps showing all v2 rules, including tool-created ones, never filtered.

### 7.2 Wizard flow — batch boundary

The v1 wizard walks the rules in the overview's order (not-migrated first, migrated last):

- **Not-migrated rules** → the normal form, exactly as today (operator page, Previous/Next, drafts, create / create-and-delete per rule).
- **"Create all rules" (+ create-and-delete-all)** appears on the **last not-migrated rule** and clones **only the not-migrated rules**; it is *not offered* from the first migrated rule onward — the option simply doesn't exist past that point (no batch button on migrated cards, no "include migrated" toggle).
- **Migrated rules** → notice card (*"Already migrated — nothing to fill."*
  - warning text from §3.3) with Previous/Next still working, plus a **"Migrate again"** action that opens the normal form for that single rule (warn-but-allow).
- after the batch completes, the return path refetches classification so freshly created rules move to the v2 side and their origins gain the badge.

### 7.3 Single rule by URL (skips the overview)

Loading an already-flagged rule shows the same warning card as §3.3 — *"This rule has already been migrated; migrating again creates a duplicate"* — but still opens the form (reviewing a migrated rule is legitimate).

---

## 8. Implementation order

| # | Work | Where | Estimate |
|---|---|---|---|
| 1 | Registry module (`src/registry/store.ts`) + config + tests | backend | 0.5 d |
| 2 | Write rows on clone / clone-and-delete / create / bulk / view-delete | backend | 0.5 d |
| 3 | `migrated` flag computation + copy-exists guard + seeder in `load-folder-classified` + tests | backend | 0.5 d |
| 4 | Click-time registry re-check in the batch path | backend | 0.25 d |
| 5 | Frontend: reorder + badge, notice cards, batch boundary, warning cards (§7) | frontend | 0.5 d |
| 6 | Compose volume + OpenShift PVC + `REGISTRY_DATA_DIR` | infra | 0.25 d |
| | **Total** | | **~2.5 days** |

Branches: stack on the split feature — backend `feature/folder-v1-v2-split` → `feature/option-e-registry`; same on the frontend. Split merges to `dev` first, then the registry branch.

---

## 9. Test checklist

1. **Old pair, first load:** folder has `CPU High` (v1) + `CPU High -v2` (v2), registry empty → after load, `CPU High` is flagged Migrated and sorted last on the v1 side; `seed_scan` row exists; reload → seeding skipped (check log line).
2. **Old pair, original deleted elsewhere:** lone `CPU High -v2` (no original in this folder) → nothing to flag on the v1 side; the v2 side shows the rule.
3. **Fresh clone without delete:** migrate `Y` → same session, reload folder → `Y` shows at the bottom of the v1 list with the Migrated badge; `Y -v2` on the v2 side.
4. **Fresh clone-and-delete:** `Z` → `Z -v2` on the v2 side, v1 side unchanged (the original is gone from Grafana, so nothing to badge).
5. **Batch boundary:** on the last not-migrated rule, "Create all rules" creates copies only for not-migrated rules (the migrated ones after it are never cloned — Grafana gains no duplicates). Past that rule, no batch button exists anywhere in the walk.
6. **Batch click-time check:** create rule 1 individually, continue the walk, click "Create all" later → rule 1 is not cloned twice.
7. **Re-migrate a flagged rule via Edit:** warning card shows, form opens, re-clone succeeds, registry gains a second row for that original.
8. **Copy-deleted → unflag:** migrate `W` (clone), then delete `W -v2` from the v2 side → reload: `W` has no badge, sits with the not-migrated rules, and the batch includes it.
9. **All v1 flagged:** every v1 rule migrated → v1 side shows the "All v1 rules in this folder have already been migrated." line above the badge rows (nothing hidden); no batch button reachable in the walk.
10. **Single-rule load of a migrated rule:** warning card shows, form opens.
11. **Hand-made `foo -v2` that fails the standard:** stays unflagged on the v1 side (seeder writes nothing).
12. **Registry restart:** restart backend → registry intact. Delete the file (test only) → badges disappear and rules reappear as normal v1; first folder re-load re-seeds them. No crash in either case.
13. **Parity:** a rule the browser calls v2 loads individually as COMPLIANT, and likewise for v1 → NON_COMPLIANT (unchanged from the split's own test). The `migrated` flag never influences COMPLIANT/NON_COMPLIANT verdicts.
14. **Regression:** the load view's single button, the two-sided overview (counts, per-row Open/Edit/Delete on both sides), row Edit entering the full v1 wizard (operator page, create-all on the last not-migrated rule) and the v2 Save-only walkthrough, drafts, per-rule create — all still work.

---

## 10. Not in this plan

- Any filtering/hiding of the v1 or v2 lists (deliberately replaced by flag + reorder + badge).
- Changing how classification works (the seeder only *uses* it).
- Changes to `migration-compliance` (no new Python endpoints).
- "Undo migration / restore original" (the registry makes it easy later — not built now).
- Bulk delete.
- Updating `openapi.yaml` (follow-up task on `main`).
