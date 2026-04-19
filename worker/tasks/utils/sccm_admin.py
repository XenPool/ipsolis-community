"""SCCM Administration Service wrapper (NTLM auth) — CLI and library.

Speaks to the Admin Service REST endpoint exposed by the SMS Provider:
    https://<SMSProvider>/AdminService/wmi/<Class>

Reads connection config from the app_config table (DATABASE_URL env var, same
approach as db_query.py — no plaintext credentials in scripts):
    sccm.base_url    e.g. https://sccm.example.com/AdminService
    sccm.username    e.g. XENPOOL\\sccm-svc (domain account for NTLM)
    sccm.password
    sccm.verify_tls  'true' | 'false' (default: true)
    sccm.site_code   e.g. P01

CLI invocation (each command returns a JSON object on stdout, exit 0 on success):
    python sccm_admin.py delete-device --name <VMName>
    python sccm_admin.py import-machine --name <VMName> --mac <MAC> --guid <GUID>
                                        --os-collection <CollectionID>
                                        [--app-collections <ID1,ID2,...>]
                                        [--resource-id-retries 60]
    python sccm_admin.py wait-task-sequence --name <VMName>
                                            --os-collection <CollectionID>
                                            [--timeout-minutes 360]
                                            [--poll-seconds 60]

Status messages use the SMS_StatMsgWithInsStrings view so log output stays
compatible with existing scripts that consume StatusDescription strings.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib.parse import quote

import psycopg2
import psycopg2.extras
import requests
from requests.adapters import HTTPAdapter
from requests_ntlm import HttpNtlmAuth
from urllib3.util.retry import Retry


# ── Config loading ────────────────────────────────────────────────────────────

def _load_config() -> dict[str, str]:
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set")
    dsn = db_url.split("+")[0] + "://" + db_url.split("://", 1)[1] if "+" in db_url else db_url
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT key, value FROM app_config WHERE key LIKE 'sccm.%%'"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    cfg = {r["key"].split(".", 1)[1]: (r["value"] or "") for r in rows}
    for required in ("base_url", "username", "password"):
        if not cfg.get(required):
            raise RuntimeError(f"app_config missing sccm.{required}")
    cfg["verify_tls"] = (cfg.get("verify_tls", "true").strip().lower() != "false")
    return cfg


# ── HTTP session ──────────────────────────────────────────────────────────────

def _session(cfg: dict) -> requests.Session:
    s = requests.Session()
    s.auth = HttpNtlmAuth(cfg["username"], cfg["password"])
    s.verify = bool(cfg["verify_tls"])
    s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=(502, 503, 504))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def _url(cfg: dict, wmi_class: str, key: str | None = None) -> str:
    root = cfg["base_url"].rstrip("/")
    if not root.lower().endswith("/adminservice"):
        root = root + "/AdminService"
    base = f"{root}/wmi/{wmi_class}"
    return f"{base}({key})" if key else base


def _get(sess: requests.Session, cfg: dict, wmi_class: str, **params: str) -> list[dict[str, Any]]:
    url = _url(cfg, wmi_class)
    resp = sess.get(url, params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("value", payload) if isinstance(payload, dict) else payload


def _get_one(sess: requests.Session, cfg: dict, wmi_class: str, key: str) -> dict[str, Any]:
    resp = sess.get(_url(cfg, wmi_class, key), timeout=30)
    resp.raise_for_status()
    return resp.json()


def _delete(sess: requests.Session, cfg: dict, wmi_class: str, key: str) -> None:
    resp = sess.delete(_url(cfg, wmi_class, key), timeout=30)
    resp.raise_for_status()


def _invoke(
    sess: requests.Session, cfg: dict, wmi_class: str, action: str,
    body: dict | None = None, key: str | None = None,
) -> dict[str, Any]:
    base = _url(cfg, wmi_class, key)
    url = f"{base}/{action}"
    resp = sess.post(url, json=body or {}, timeout=60)
    resp.raise_for_status()
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


# ── Lookups ───────────────────────────────────────────────────────────────────

def _find_devices_by_name(sess: requests.Session, cfg: dict, name: str) -> list[dict[str, Any]]:
    filt = f"Name eq '{name}'"
    return _get(
        sess, cfg, "SMS_R_System",
        **{"$filter": filt, "$select": "ResourceID,Name"},
    )


def _find_deployment_id_for_collection(
    sess: requests.Session, cfg: dict, collection_id: str,
) -> str | None:
    filt = f"CollectionID eq '{collection_id}'"
    rows = _get(
        sess, cfg, "SMS_Deployment",
        **{"$filter": filt, "$select": "DeploymentID,CollectionName"},
    )
    if not rows:
        return None
    return rows[0].get("DeploymentID")


# ── Operations ────────────────────────────────────────────────────────────────

def op_delete_device(name: str) -> dict[str, Any]:
    cfg = _load_config()
    sess = _session(cfg)
    devices = _find_devices_by_name(sess, cfg, name)
    if len(devices) > 1:
        return {
            "success": False,
            "error": f"Multiple SCCM devices found for name={name!r} (count={len(devices)}); refusing to delete.",
            "duplicates": [{"ResourceID": d.get("ResourceID"), "Name": d.get("Name")} for d in devices],
        }
    if not devices:
        return {"success": True, "deleted": False, "message": f"No SCCM device with name {name!r} (already removed)."}
    resource_id = devices[0]["ResourceID"]
    _delete(sess, cfg, "SMS_R_System", str(resource_id))
    return {
        "success": True,
        "deleted": True,
        "resource_id": resource_id,
        "name": name,
        "message": f"Deleted SCCM device {name!r} (ResourceID={resource_id}).",
    }


def op_import_machine(
    name: str, mac: str, guid: str,
    os_collection_id: str, app_collection_ids: list[str],
    resource_id_retries: int = 60, retry_sleep: int = 60,
) -> dict[str, Any]:
    cfg = _load_config()
    sess = _session(cfg)

    # Safety: must be <=1 device with this name
    existing = _find_devices_by_name(sess, cfg, name)
    if len(existing) > 1:
        return {
            "success": False,
            "error": f"Multiple SCCM devices found for name={name!r}; refusing to import.",
            "duplicates": [d.get("ResourceID") for d in existing],
        }

    resource_id = existing[0]["ResourceID"] if existing else None
    imported = False

    if resource_id is None:
        # ImportMachineEntry is a static method on SMS_Site; admin-service exposes
        # it as a POST on the class itself (no key)
        _invoke(
            sess, cfg, "SMS_Site", "ImportMachineEntry",
            body={
                "MachineName": name,
                "SMBIOSGUID": guid or "",
                "MACAddress": mac or "",
                "OverwriteExistingRecord": False,
            },
        )
        imported = True

        # Poll for the ResourceID to appear
        for attempt in range(1, resource_id_retries + 1):
            time.sleep(retry_sleep)
            devs = _find_devices_by_name(sess, cfg, name)
            if len(devs) > 1:
                return {
                    "success": False,
                    "error": f"Multiple SCCM devices appeared during ResourceID polling for {name!r}.",
                }
            if devs:
                resource_id = devs[0]["ResourceID"]
                break

    if resource_id is None:
        return {
            "success": False,
            "error": f"Import submitted but ResourceID never appeared within {resource_id_retries * retry_sleep}s.",
        }

    # Add to application collections
    added = []
    for col_id in app_collection_ids:
        rule = {
            "ResourceClassName": "SMS_R_System",
            "RuleName": f"XenPool-Auto-{name}",
            "ResourceID": int(resource_id),
        }
        _invoke(
            sess, cfg, "SMS_Collection", "AddMembershipRule",
            key=f"'{col_id}'",
            body={"collectionRule": rule},
        )
        added.append(col_id)

    # Trigger collection refreshes (OS + app collections)
    refresh_targets = [os_collection_id, *app_collection_ids]
    for col_id in refresh_targets:
        try:
            _invoke(sess, cfg, "SMS_Collection", "RequestRefresh", key=f"'{col_id}'")
        except requests.HTTPError as exc:
            # Best-effort — don't fail import if refresh errors
            sys.stderr.write(f"warn: RequestRefresh failed for {col_id}: {exc}\n")

    return {
        "success": True,
        "imported": imported,
        "resource_id": resource_id,
        "name": name,
        "os_collection": os_collection_id,
        "app_collections_added": added,
    }


# SMS_StatMsgWithInsStrings message IDs we map to human statuses.
# Values come from SCCM's MRE / SMS_TSServerExtension status message set and
# are stable across CB versions. Extend as needed.
_TS_MESSAGE_STATUSES: dict[int, tuple[str, str]] = {
    # MessageID: (status, short label)
    11170: ("running", "Task sequence execution engine started execution"),
    11171: ("running", "Task sequence execution engine successfully completed action"),
    11140: ("running", "Task sequence started"),
    11143: ("failed",  "Task sequence failed with error code"),
    11124: ("failed",  "Task sequence action exited with error"),
    11141: ("success", "Task sequence completed successfully"),
    # "The task sequence manager successfully completed execution of the task sequence"
    11172: ("success", "Task sequence manager successfully completed execution of the task sequence"),
}


def _latest_status_message(
    sess: requests.Session, cfg: dict, machine_name: str,
) -> dict[str, Any] | None:
    filt = (
        f"MachineName eq '{machine_name}'"
    )
    rows = _get(
        sess, cfg, "SMS_StatMsgWithInsStrings",
        **{
            "$filter": filt,
            "$orderby": "Time desc",
            "$top": "5",
        },
    )
    if not rows:
        return None
    return rows[0]


def _per_device_deployment_status(
    sess: requests.Session, cfg: dict, deployment_id: str, resource_id: int,
) -> dict[str, Any] | None:
    """Fetch per-device task-sequence deployment status (StatusType/Description).

    SMS_DPMDeploymentAssetDetails has composite key (AssignmentID, MachineID).
    StatusType values: 1=Success, 2=InProgress, 3=Error, 4=Unknown.
    """
    try:
        filt = f"AssignmentID eq '{deployment_id}' and MachineID eq {int(resource_id)}"
        rows = _get(
            sess, cfg, "SMS_DPMDeploymentAssetDetails",
            **{"$filter": filt, "$top": "1"},
        )
        return rows[0] if rows else None
    except requests.HTTPError:
        return None


def op_wait_task_sequence(
    name: str, os_collection_id: str,
    timeout_minutes: int = 360, poll_seconds: int = 60,
) -> dict[str, Any]:
    cfg = _load_config()
    sess = _session(cfg)

    deployment_id = _find_deployment_id_for_collection(sess, cfg, os_collection_id)
    if not deployment_id:
        return {"success": False, "error": f"No deployment found for collection {os_collection_id}."}

    devices = _find_devices_by_name(sess, cfg, name)
    if not devices:
        return {"success": False, "error": f"SCCM device {name!r} not found."}
    if len(devices) > 1:
        return {"success": False, "error": f"Multiple SCCM devices for {name!r}; cannot monitor."}
    resource_id = devices[0]["ResourceID"]

    started = time.time()
    max_seconds = timeout_minutes * 60
    last_status_msg: dict[str, Any] | None = None
    last_per_device: dict[str, Any] | None = None
    loops = 0

    while (time.time() - started) < max_seconds:
        loops += 1
        last_status_msg = _latest_status_message(sess, cfg, name)
        last_per_device = _per_device_deployment_status(sess, cfg, deployment_id, resource_id)

        status_type = (last_per_device or {}).get("StatusType")
        status_desc = (last_per_device or {}).get("StatusDescription") or ""
        msg_id = (last_status_msg or {}).get("MessageID")
        mapped = _TS_MESSAGE_STATUSES.get(int(msg_id)) if msg_id is not None else None
        mapped_state = mapped[0] if mapped else None
        mapped_label = mapped[1] if mapped else None

        # Success: StatusType 1 OR an explicit success-mapped message
        if status_type == 1 or mapped_state == "success":
            return {
                "success": True,
                "result": "Available",
                "deployment_id": deployment_id,
                "resource_id": resource_id,
                "status_type": status_type,
                "status_description": status_desc or mapped_label or "",
                "message_id": msg_id,
                "loops": loops,
            }

        # Failure: StatusType 3 OR explicit failure-mapped message
        if status_type == 3 or mapped_state == "failed":
            return {
                "success": False,
                "result": "TaskSeqRunError",
                "error": status_desc or mapped_label or "Task sequence failed",
                "deployment_id": deployment_id,
                "resource_id": resource_id,
                "status_type": status_type,
                "status_description": status_desc,
                "message_id": msg_id,
                "loops": loops,
            }

        time.sleep(poll_seconds)

    # Timeout
    return {
        "success": False,
        "result": "TaskSeqTimeout",
        "error": f"Task sequence did not complete within {timeout_minutes} minutes.",
        "deployment_id": deployment_id,
        "resource_id": resource_id,
        "status_type": (last_per_device or {}).get("StatusType"),
        "status_description": (last_per_device or {}).get("StatusDescription"),
        "message_id": (last_status_msg or {}).get("MessageID"),
        "loops": loops,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> int:
    parser = argparse.ArgumentParser(prog="sccm_admin")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_del = sub.add_parser("delete-device")
    p_del.add_argument("--name", required=True)

    p_imp = sub.add_parser("import-machine")
    p_imp.add_argument("--name", required=True)
    p_imp.add_argument("--mac", default="")
    p_imp.add_argument("--guid", default="")
    p_imp.add_argument("--os-collection", required=True)
    p_imp.add_argument("--app-collections", default="",
                       help="Comma- or semicolon-separated list of collection IDs")
    p_imp.add_argument("--resource-id-retries", type=int, default=60)
    p_imp.add_argument("--retry-sleep", type=int, default=60)

    p_wait = sub.add_parser("wait-task-sequence")
    p_wait.add_argument("--name", required=True)
    p_wait.add_argument("--os-collection", required=True)
    p_wait.add_argument("--timeout-minutes", type=int, default=360)
    p_wait.add_argument("--poll-seconds", type=int, default=60)

    args = parser.parse_args()

    try:
        if args.cmd == "delete-device":
            out = op_delete_device(args.name)
        elif args.cmd == "import-machine":
            raw = (args.app_collections or "").replace(";", ",")
            app_list = [c.strip() for c in raw.split(",") if c.strip()]
            out = op_import_machine(
                name=args.name, mac=args.mac, guid=args.guid,
                os_collection_id=args.os_collection, app_collection_ids=app_list,
                resource_id_retries=args.resource_id_retries,
                retry_sleep=args.retry_sleep,
            )
        elif args.cmd == "wait-task-sequence":
            out = op_wait_task_sequence(
                name=args.name, os_collection_id=args.os_collection,
                timeout_minutes=args.timeout_minutes, poll_seconds=args.poll_seconds,
            )
        else:
            out = {"success": False, "error": f"Unknown command {args.cmd!r}"}
    except requests.HTTPError as exc:
        body = ""
        try:
            body = exc.response.text[:500] if exc.response is not None else ""
        except Exception:
            pass
        out = {"success": False, "error": f"HTTP {exc.response.status_code if exc.response else '??'}: {exc}", "response": body}
    except Exception as exc:  # noqa: BLE001 — we want JSON-shaped errors for any failure
        out = {"success": False, "error": str(exc)}

    print(json.dumps(out, default=str))
    return 0 if out.get("success") else 1


if __name__ == "__main__":
    sys.exit(_cli())
