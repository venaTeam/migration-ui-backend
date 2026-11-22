"""The migrated flag and the one-time seeder (docs/PLAN-v1-migrated-flag.md sections 3 and 6).

migrated = a registry row exists for the original uid AND that row's created copy
is still among the folder's v2 rules (the copy-exists guard). Computed at every
folder load, never stored.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass

from app.registry.store import Registry
from app.standard import StandardSchema, classify_rule_against_schema

log = logging.getLogger(__name__)

# JavaScript's `/^(.*?)-v2$/`: "." stops at line terminators.
_JS_LINE_TERMINATORS = "\n\r" + chr(0x2028) + chr(0x2029)
_V2_TITLE = re.compile(f"([^{_JS_LINE_TERMINATORS}]*?)-v2")


@dataclass(frozen=True)
class RegistryRule:
    uid: str
    title: str
    labels: Mapping[str, str]
    annotations: Mapping[str, str]


def base_title_of(title: str) -> str | None:
    """Title a "-v2" clone was made from, one hop at a time.

    Accepts the tool's "CPU High-v2" and the hand-typed "CPU High -v2";
    "CPU High-v2-v2" gives "CPU High-v2", and "CPU High" gives None.
    """
    match = _V2_TITLE.fullmatch(title)
    if match is None:
        return None
    base = match.group(1)
    return base[:-1] if base.endswith(" ") else base


def seed_folder(
    registry: Registry, folder_uid: str, rules: list[RegistryRule], schema: StandardSchema
) -> int:
    """One seeder pass over a folder's rules; returns the number of rows written.

    A row needs both signals: the "-v2" title shape and a compliant clone. A chain
    gives one row per hop. The pass, its rows and the seed_scan mark share one
    transaction, so concurrent first loads of a folder can't both seed it.
    """
    with registry.transaction() as tx:
        if tx.is_folder_seeded(folder_uid):
            log.info("[registry] folder %s already seeded — skipping scan", folder_uid)
            return 0

        by_title = {rule.title: rule for rule in rules}
        # One row per (original, created) hop, ever.
        written = {
            (row.original_uid or "", row.created_uid)
            for row in tx.migrations_for_folder(folder_uid)
        }
        pairs_found = 0

        def record(
            original_uid: str | None,
            original_title: str,
            created: RegistryRule,
            deleted_original: bool,
        ) -> None:
            nonlocal pairs_found
            key = (original_uid or "", created.uid)
            if key in written:
                return
            written.add(key)
            tx.insert_migration(
                original_uid=original_uid,
                original_title=original_title,
                created_uid=created.uid,
                created_title=created.title,
                folder_uid=folder_uid,
                deleted_original=deleted_original,
                created_via="seed",
                migrated_by=None,
            )
            pairs_found += 1

        for rule in rules:
            base = base_title_of(rule.title)
            # A rule titled exactly "-v2" has no original title behind it.
            if not base:
                continue
            # A hand-made "-v2" that fails the standard was not made by this tool.
            if classify_rule_against_schema(schema, rule.labels, rule.annotations):
                continue

            created = rule
            parent = by_title.get(base)
            while parent is not None:
                record(parent.uid, parent.title, created, False)
                created = parent
                next_base = base_title_of(parent.title)
                parent = by_title.get(next_base) if next_base is not None else None

            # Lone clone: the original was deleted before this first scan.
            if created is rule:
                record(None, base, rule, True)

        tx.mark_folder_seeded(folder_uid, pairs_found)
    log.info("[registry] seeded folder %s: %d pair(s) written", folder_uid, pairs_found)
    return pairs_found


def compute_migrated_flags(
    registry: Registry, folder_uid: str, v2_uids: set[str]
) -> dict[str, bool]:
    """The migrated flag per original uid, from the registry and the folder's v2 uids."""
    flags: dict[str, bool] = {}
    for row in registry.migrations_for_folder(folder_uid):
        if not row.original_uid:
            continue
        flags[row.original_uid] = flags.get(row.original_uid, False) or row.created_uid in v2_uids
    return flags
