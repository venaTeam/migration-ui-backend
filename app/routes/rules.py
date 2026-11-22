"""Rule routes: load, folder views, rule lists, clone, create and in-place edits of v2 rules.

Every Grafana call uses the admin account, after check_folder_permission has
confirmed the user could do the same in Grafana: VIEW to read, EDIT to change.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.aio import gather_limited
from app.deps import Caller, CallerDep, Services, ServicesDep, require_session
from app.errors import ApiError, compliance_error, error_response, grafana_error
from app.grafana.client import GrafanaClient
from app.grafana.errors import FolderNotFoundError, RuleNotFoundError
from app.jsonio import dumps
from app.permissions import FolderPermission, check_folder_permission
from app.registry.flags import RegistryRule, compute_migrated_flags, seed_folder
from app.standard import StandardSchema, classify_rule_against_schema, validate_clone_payload
from app.urls import parse_folder_uid, parse_rule_uid, rule_view_url
from app.validation import NonEmptyStr, StrictBody, StringRecord, parse_body

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rules", dependencies=[Depends(require_session)])

UNTITLED_RULE = "Untitled rule"
REQUEST_FAILED = "Request failed"
NO_EDIT_PERMISSION = (
    "You do not have Edit/Admin permission for the folder this alert rule belongs to."
)
NO_FOLDER_ACCESS = "You don't have access to this folder."
NO_RULE_FOLDER_ACCESS = "You don't have access to this rule's folder."
NOT_A_RULE_URL = "Not a Grafana rule URL."
MAX_LOAD_LIST = 50
LOAD_LIST_MESSAGE = f"rule_urls must list 1 to {MAX_LOAD_LIST} rule URLs."


class LoadBody(StrictBody):
    rule_url: NonEmptyStr


class LoadListBody(StrictBody):
    rule_urls: list[str]


class FolderUrlBody(StrictBody):
    folder_url: NonEmptyStr


class CloneBody(StrictBody):
    labels: StringRecord
    annotations: StringRecord
    folder_url: NonEmptyStr | None = None


class CreateRuleBody(StrictBody):
    folder_url: NonEmptyStr
    title: NonEmptyStr
    labels: StringRecord
    annotations: StringRecord


class ViewUpdateBody(StrictBody):
    title: NonEmptyStr
    labels: StringRecord
    annotations: StringRecord
    # Annotation keys the user emptied; removed rather than kept by the merge.
    clearAnnotations: list[str] | None = None  # noqa: N815 (the frontend's field name)
    # Label keys to remove, e.g. the previous "Appchi - <operator>" key.
    clearLabels: list[str] | None = None  # noqa: N815


def _rule_result(action: str, uid: Any, rule_url: str, title: str) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "ok", "action": action}
    # JSON.stringify dropped an undefined rule_uid.
    if uid is not None:
        result["rule_uid"] = uid
    result.update(rule_url=rule_url, title=title)
    return result


def _rule_error(exc: Exception, route: str) -> JSONResponse:
    log.error("[%s] error: %s", route, exc)
    return grafana_error(exc, "Clone failed")


async def _allowed(
    services: Services, folder_uid: str, caller: Caller, level: FolderPermission
) -> bool:
    return await check_folder_permission(services.admin, folder_uid, caller, level)


async def _copy_exists(admin: GrafanaClient, uid: str) -> bool:
    """Only a 404 means the copy is gone; other failures propagate."""
    try:
        await admin.get_rule(uid)
    except RuleNotFoundError:
        return False
    return True


@router.post("/load")
async def load_rule(request: Request, caller: CallerDep, services: ServicesDep) -> JSONResponse:
    body = await parse_body(request, LoadBody, "rule_url is required.")
    uid = parse_rule_uid(body.rule_url)
    if uid is None:
        return error_response(400, "Bad Request", NOT_A_RULE_URL)
    try:
        rule = await services.admin.get_rule(uid)
        folder_uid = rule.get("folderUID") or ""
        if not folder_uid or not await _allowed(
            services, folder_uid, caller, FolderPermission.VIEW
        ):
            raise ApiError(403, "Forbidden", NO_RULE_FOLDER_ACCESS)

        # The UID, so the service evaluates exactly the rule checked above.
        response = await services.compliance.evaluate(uid)
        data = response.json()
        if response.is_success and isinstance(data, dict) and isinstance(data.get("rule_uid"), str):
            if data.get("folder_uid") is None:
                data["folder_uid"] = folder_uid
            # An already-migrated rule still opens; the frontend shows a warning. On a
            # failed copy check the flag stays unset (a missing badge is recoverable).
            previous = services.registry.latest_migration_for_original(data["rule_uid"])
            if previous is not None and previous.created_uid:
                try:
                    if await _copy_exists(services.admin, previous.created_uid):
                        data["migrated"] = True
                except Exception as exc:
                    log.warning(
                        "[load] copy-exists check for %s failed: %s", previous.created_uid, exc
                    )
        return JSONResponse(data, status_code=response.status_code)
    except Exception as exc:
        return compliance_error(exc)


@router.post("/load-list")
async def load_rule_list(
    request: Request, caller: CallerDep, services: ServicesDep
) -> JSONResponse:
    """Several rules at once: the ones that can be migrated, and why the others can't."""
    body = await parse_body(request, LoadListBody, LOAD_LIST_MESSAGE)
    rule_urls = list(dict.fromkeys(body.rule_urls))
    if not 1 <= len(rule_urls) <= MAX_LOAD_LIST:
        return error_response(400, "Bad Request", LOAD_LIST_MESSAGE)

    folder_access: dict[str, bool] = {}
    folder_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def can_view(folder_uid: str) -> bool:
        # One permission check per folder, however many rules share it.
        async with folder_locks[folder_uid]:
            if folder_uid not in folder_access:
                try:
                    folder_access[folder_uid] = await _allowed(
                        services, folder_uid, caller, FolderPermission.VIEW
                    )
                except FolderNotFoundError:
                    folder_access[folder_uid] = False
            return folder_access[folder_uid]

    try:
        schema = await services.standard_schema.get()
        entries = await gather_limited(
            partial(_list_entry, services, schema, url, can_view) for url in rule_urls
        )
    except Exception as exc:
        log.error("[load-list] error: %s", exc)
        return grafana_error(exc, "Rule list load failed")
    return JSONResponse(
        {
            "rules": [entry for is_rule, entry in entries if is_rule],
            "skipped": [entry for is_rule, entry in entries if not is_rule],
        }
    )


async def _list_entry(
    services: Services,
    schema: StandardSchema,
    rule_url: str,
    can_view: Callable[[str], Awaitable[bool]],
) -> tuple[bool, dict[str, Any]]:
    """(True, rule) for a rule that can be migrated, else (False, why not)."""

    def skipped(reason: str, message: str) -> tuple[bool, dict[str, Any]]:
        return False, {"rule_url": rule_url, "reason": reason, "message": message}

    uid = parse_rule_uid(rule_url)
    if uid is None:
        return skipped("invalid_url", NOT_A_RULE_URL)
    try:
        rule = await services.admin.get_rule(uid)
    except RuleNotFoundError:
        return skipped("not_found", "Rule not found in Grafana.")
    folder_uid = rule.get("folderUID") or ""
    if not folder_uid or not await can_view(folder_uid):
        return skipped("forbidden", NO_RULE_FOLDER_ACCESS)
    if not classify_rule_against_schema(
        schema, rule.get("labels") or {}, rule.get("annotations") or {}
    ):
        return skipped("already_v2", "Already on the v2 standard.")
    previous = services.registry.latest_migration_for_original(uid)
    if (
        previous is not None
        and previous.created_uid
        and await _copy_exists(services.admin, previous.created_uid)
    ):
        return skipped("migrated", "Already migrated by this tool.")
    return True, {
        "uid": uid,
        "title": rule.get("title") or UNTITLED_RULE,
        "rule_url": rule_view_url(services.settings.grafana_url, uid),
        "folder_uid": folder_uid,
    }


async def _viewable_folder(
    services: Services, caller: Caller, folder_url: str
) -> str | JSONResponse:
    """The folder's UID once the user may view it, or the error to answer."""
    folder_uid = parse_folder_uid(folder_url)
    if not folder_uid:
        return error_response(400, "Bad Request", "Could not parse folder UID from URL.")
    if not await _allowed(services, folder_uid, caller, FolderPermission.VIEW):
        return error_response(403, "Forbidden", NO_FOLDER_ACCESS)
    return folder_uid


@router.post("/load-folder")
async def load_folder(request: Request, caller: CallerDep, services: ServicesDep) -> JSONResponse:
    body = await parse_body(request, FolderUrlBody, "folder_url is required.")
    try:
        folder_uid = await _viewable_folder(services, caller, body.folder_url)
        if isinstance(folder_uid, JSONResponse):
            return folder_uid
        rules = await services.admin.list_rules_in_folder(folder_uid)
        items = [
            {
                "uid": rule.get("uid"),
                "title": rule.get("title") or UNTITLED_RULE,
                "rule_url": rule_view_url(services.settings.grafana_url, rule.get("uid")),
            }
            for rule in rules
        ]
        return JSONResponse({"folder_uid": folder_uid, "rules": items})
    except Exception as exc:
        log.error("[load-folder] error: %s", exc)
        return grafana_error(exc, "Folder load failed")


@router.post("/load-folder-classified")
async def load_folder_classified(
    request: Request, caller: CallerDep, services: ServicesDep
) -> JSONResponse:
    body = await parse_body(request, FolderUrlBody, "folder_url is required.")
    try:
        folder_uid = await _viewable_folder(services, caller, body.folder_url)
        if isinstance(folder_uid, JSONResponse):
            return folder_uid
        schema = await services.standard_schema.get()
        rules = await services.admin.list_rules_in_folder(folder_uid)

        v1: list[dict[str, Any]] = []
        v2: list[dict[str, Any]] = []
        for rule in rules:
            uid = rule.get("uid")
            failed = classify_rule_against_schema(
                schema, rule.get("labels") or {}, rule.get("annotations") or {}
            )
            summary: dict[str, Any] = {
                "uid": uid,
                "title": rule.get("title") or UNTITLED_RULE,
                "rule_url": rule_view_url(services.settings.grafana_url, uid),
            }
            if failed:
                summary["missing_fields"] = failed
            (v1 if failed else v2).append(summary)

        # One-time seeder pass on the first load of this folder (plan section 6),
        # over the rules fetched above; no extra Grafana calls.
        try:
            seed_folder(
                services.registry,
                folder_uid,
                [
                    RegistryRule(
                        uid=rule.get("uid"),  # type: ignore[arg-type]
                        title=rule.get("title") or UNTITLED_RULE,
                        labels=rule.get("labels") or {},
                        annotations=rule.get("annotations") or {},
                    )
                    for rule in rules
                ],
                schema,
            )
        except Exception:
            # A registry failure must never break the folder load.
            log.warning("[load-folder-classified] seeding failed", exc_info=True)

        flags = compute_migrated_flags(services.registry, folder_uid, {rule["uid"] for rule in v2})
        for rule in v1:
            if flags.get(rule["uid"]):
                rule["migrated"] = True
        # Migrated rules sink to the end of the v1 list; nothing is hidden.
        v1.sort(key=lambda rule: bool(rule.get("migrated")))

        return JSONResponse(
            {"folder_uid": folder_uid, "counts": {"v1": len(v1), "v2": len(v2)}, "v1": v1, "v2": v2}
        )
    except Exception as exc:
        log.error("[load-folder-classified] error: %s", exc)
        return grafana_error(exc, "Folder load failed")


async def _editable_rule(
    services: Services, caller: Caller, uid: str, route: str
) -> tuple[dict[str, Any], str]:
    """The rule and its folder, once the user may edit that folder."""
    log.info("[%s] fetching rule %s", route, uid)
    rule = await services.admin.get_rule(uid)
    folder_uid = rule.get("folderUID") or ""
    if not folder_uid:
        raise ApiError(400, "Bad Request", "Rule has no folder UID.")
    if not await _allowed(services, folder_uid, caller, FolderPermission.EDIT):
        raise ApiError(403, "Forbidden", NO_EDIT_PERMISSION)
    return rule, folder_uid


@router.delete("/view/{uid}")
async def delete_rule_view(uid: str, caller: CallerDep, services: ServicesDep) -> JSONResponse:
    try:
        rule, folder_uid = await _editable_rule(services, caller, uid, "view-delete")
        log.info("[view-delete] deleting rule %s", uid)
        await services.admin.delete_rule(uid)
        # Audit note only: the copy-exists guard makes the original migratable again.
        services.registry.record_view_delete(uid, rule.get("title") or "", folder_uid, caller.login)
        return JSONResponse({"status": "ok", "deleted": uid})
    except Exception as exc:
        return _rule_error(exc, "view-delete")


@router.put("/view/{uid}")
async def update_rule_view(
    uid: str, request: Request, caller: CallerDep, services: ServicesDep
) -> JSONResponse:
    """Edit a v2 rule in place; the edit must keep it v2."""
    body = await parse_body(request, ViewUpdateBody, "title, labels and annotations are required.")
    try:
        schema = await services.standard_schema.get()
        failed = classify_rule_against_schema(schema, body.labels, body.annotations)
        if failed:
            raise ApiError(
                400,
                "Validation Error",
                f"This edit would take the rule out of v2. Failing fields: {', '.join(failed)}",
            )
        rule, _folder_uid = await _editable_rule(services, caller, uid, "view-update")

        # Merge over the rule's own labels/annotations so keys the standard doesn't
        # know (team tags, ...) survive; cleared keys are removed.
        labels = dict(rule.get("labels") or {})
        for key, value in body.labels.items():
            if value != "":
                labels[key] = value
        for key in body.clearLabels or []:
            labels.pop(key, None)
        annotations = dict(rule.get("annotations") or {})
        for key, value in body.annotations.items():
            if value != "":
                annotations[key] = value
            else:
                annotations.pop(key, None)
        for key in body.clearAnnotations or []:
            annotations.pop(key, None)

        # Only what the form edits; condition, queries, group and folder stay.
        updated = {**rule, "title": body.title, "labels": labels, "annotations": annotations}
        log.info("[view-update] updating rule %s", uid)
        result = await services.admin.update_rule(uid, updated)
        result_uid = result.get("uid") or uid
        return JSONResponse(
            {
                "status": "ok",
                "updated": result_uid,
                "title": body.title,
                "rule_url": rule_view_url(services.settings.grafana_url, result_uid),
            }
        )
    except Exception as exc:
        return _rule_error(exc, "view-update")


def build_cloned_rule(
    original: dict[str, Any],
    labels: dict[str, str],
    annotations: dict[str, str],
    folder_uid: str | None,
) -> dict[str, Any]:
    new_title = f"{original.get('title') or 'Alert rule'}-v2"
    # Labels come only from the form (the standard fields); annotations are merged
    # so an existing message or summary is kept unless overridden.
    merged_annotations = {**(original.get("annotations") or {}), **annotations}
    # Without its identifiers Grafana treats the rule as new.
    rule = {
        key: value
        for key, value in original.items()
        if key not in ("uid", "id", "updated", "updatedBy", "provenance")
    }
    if folder_uid:
        rule["folderUID"] = folder_uid
    return {**rule, "title": new_title, "labels": labels, "annotations": merged_annotations}


def build_skeleton_rule(
    folder_uid: str, title: str, labels: dict[str, str], annotations: dict[str, str]
) -> dict[str, Any]:
    return {
        "title": title,
        "folderUID": folder_uid,
        "ruleGroup": "default",
        "condition": "A",
        "data": [
            {
                "refId": "A",
                "queryType": "",
                "relativeTimeRange": {"from": 300, "to": 0},
                "datasourceUid": "__expr__",
                "model": {"type": "math", "expression": "vector(0) == 1", "refId": "A"},
            }
        ],
        "noDataState": "NoData",
        "execErrState": "Alerting",
        "for": "0",
        "labels": labels,
        "annotations": annotations,
    }


async def _perform_clone(
    services: Services, caller: Caller, uid: str, body: CloneBody, *, delete_original: bool
) -> tuple[dict[str, Any], int | None]:
    """Clone a rule as its "-v2" copy; returns the answer and the registry row id."""
    schema = await services.standard_schema.get()
    missing = validate_clone_payload(schema, body.labels, body.annotations)
    if missing:
        raise ApiError(400, REQUEST_FAILED, f"Missing required fields: {', '.join(missing)}")

    admin = services.admin
    log.info("[clone] fetching original rule %s", uid)
    original = await admin.get_rule(uid)
    log.info("[clone] fetched rule title: %s", original.get("title"))

    # Click-time registry re-check: a rule migrated moments ago (same batch, or
    # another user) is not cloned again while its copy exists. Only a 404 means
    # the copy is gone; any other failure blocks, so nothing duplicates silently.
    previous = services.registry.latest_migration_for_original(uid)
    if previous is not None and previous.created_uid:
        try:
            exists = await _copy_exists(admin, previous.created_uid)
        except Exception as exc:
            log.warning(
                "[clone] copy-exists check for %s failed — blocking re-clone to stay safe: %s",
                previous.created_uid,
                exc,
            )
            raise ApiError(
                502,
                REQUEST_FAILED,
                "Could not verify whether this rule's v2 copy still exists. Try again shortly.",
            ) from exc
        if exists:
            raise ApiError(
                409,
                REQUEST_FAILED,
                f"This rule has already been migrated (copy: {previous.created_title}). "
                "Delete the v2 copy first if you want to migrate it again.",
            )

    source_folder_uid = original.get("folderUID") or ""
    if not source_folder_uid:
        raise ApiError(400, REQUEST_FAILED, "Original rule has no folder UID.")
    dest_folder_uid = parse_folder_uid(body.folder_url) if body.folder_url else source_folder_uid
    if not dest_folder_uid:
        raise ApiError(
            400, REQUEST_FAILED, "Could not parse folder UID from the destination folder URL."
        )

    # All checks before anything changes: EDIT on the source when it will be deleted,
    # VIEW on it when the copy goes elsewhere, and EDIT on the destination.
    same_folder = dest_folder_uid == source_folder_uid
    if delete_original:
        if not await _allowed(services, source_folder_uid, caller, FolderPermission.EDIT):
            raise ApiError(
                403,
                "Forbidden",
                "You do not have Edit/Admin permission for the folder the original alert rule belongs to.",
            )
    elif not same_folder and not await _allowed(
        services, source_folder_uid, caller, FolderPermission.VIEW
    ):
        raise ApiError(
            403, REQUEST_FAILED, "You do not have access to the folder this alert rule belongs to."
        )
    if not (delete_original and same_folder) and not await _allowed(
        services, dest_folder_uid, caller, FolderPermission.EDIT
    ):
        where = "this alert rule belongs to" if same_folder else "chosen as the destination"
        raise ApiError(
            403, REQUEST_FAILED, f"You do not have Edit/Admin permission for the folder {where}."
        )

    cloned = build_cloned_rule(original, body.labels, body.annotations, dest_folder_uid)
    new_title = cloned["title"]

    log.info("[clone] checking for existing rule titled: %s", new_title)
    # Title match in the destination folder only: a global match could overwrite
    # an unrelated rule in a folder the caller can't see (the admin account can).
    existing = next(
        (
            rule
            for rule in await admin.list_rules_in_folder(dest_folder_uid)
            if rule.get("title") == new_title
        ),
        None,
    )
    if existing is not None and existing.get("uid"):
        log.info("[clone] updating existing rule %s in destination folder", existing["uid"])
        result = await admin.update_rule(existing["uid"], {**cloned, "uid": existing["uid"]})
        action = "updated"
    else:
        log.info("[clone] creating new rule with title: %s", new_title)
        result = await admin.create_editable_rule(cloned)
        action = "created"
    log.info("[clone] %s rule response: %s", action, result)

    grafana_url = services.settings.grafana_url
    new_uid = result.get("uid") or (existing or {}).get("uid")
    rule_url = (
        rule_view_url(grafana_url, new_uid)
        if new_uid
        else f"{grafana_url.removesuffix('/')}/alerting/list"
    )

    # Recorded as soon as the copy exists, so a later failure can't leave it unrecorded.
    row_id = None
    if new_uid:
        row_id = services.registry.record_migration(
            original_uid=original.get("uid") or None,
            original_title=original.get("title") or None,
            created_uid=new_uid,
            created_title=new_title,
            folder_uid=source_folder_uid if delete_original else dest_folder_uid,
            deleted_original=False,
            created_via="clone-and-delete" if delete_original else "clone",
            migrated_by=caller.login,
        )
    return _rule_result(action, new_uid, rule_url, new_title), row_id


@router.post("/{uid}/clone")
async def clone_rule(
    uid: str, request: Request, caller: CallerDep, services: ServicesDep
) -> JSONResponse:
    body = await parse_body(request, CloneBody, "labels and annotations are required.")
    try:
        result, _row_id = await _perform_clone(services, caller, uid, body, delete_original=False)
        return JSONResponse(result)
    except Exception as exc:
        return _rule_error(exc, "clone")


@router.post("/{uid}/clone-and-delete")
async def clone_and_delete_rule(
    uid: str, request: Request, caller: CallerDep, services: ServicesDep
) -> JSONResponse:
    body = await parse_body(request, CloneBody, "labels and annotations are required.")
    try:
        result, row_id = await _perform_clone(services, caller, uid, body, delete_original=True)
        log.info("[clone-and-delete] deleting original rule %s", uid)
        await services.admin.delete_rule(uid)
        if row_id is not None:
            services.registry.mark_original_deleted(row_id)
        return JSONResponse({**result, "original_deleted": True})
    except Exception as exc:
        return _rule_error(exc, "clone-and-delete")


@router.post("/create")
async def create_rule(request: Request, caller: CallerDep, services: ServicesDep) -> JSONResponse:
    body = await parse_body(
        request, CreateRuleBody, "folder_url, title, labels and annotations are required."
    )
    log.info("[create] request: %s", dumps(body.model_dump()))
    try:
        schema = await services.standard_schema.get()
        missing = validate_clone_payload(schema, body.labels, body.annotations)
        if missing:
            raise ApiError(
                400, "Validation Error", f"Missing required fields: {', '.join(missing)}"
            )

        folder_uid = parse_folder_uid(body.folder_url)
        if not folder_uid:
            raise ApiError(400, "Bad Request", "Could not parse folder UID from URL.")
        log.info("[create] folderUid: %s", folder_uid)
        if not await _allowed(services, folder_uid, caller, FolderPermission.EDIT):
            raise ApiError(
                403, "Forbidden", "You do not have Edit/Admin permission for this folder."
            )

        rule = build_skeleton_rule(folder_uid, body.title, body.labels, body.annotations)
        log.info("[create] rule payload: %s", dumps(rule))
        result = await services.admin.create_editable_rule(rule)
        log.info("[create] grafana response: %s", dumps(result))
        new_uid = result.get("uid")
        grafana_url = services.settings.grafana_url
        rule_url = (
            rule_view_url(grafana_url, new_uid)
            if new_uid
            else f"{grafana_url.removesuffix('/')}/alerting/list"
        )

        # A brand-new rule has no original; the row is history.
        if new_uid:
            services.registry.record_migration(
                original_uid=None,
                original_title=None,
                created_uid=new_uid,
                created_title=body.title,
                folder_uid=folder_uid,
                deleted_original=False,
                created_via="create",
                migrated_by=caller.login,
            )
        return JSONResponse(_rule_result("created", new_uid, rule_url, body.title))
    except Exception as exc:
        return _rule_error(exc, "create")
