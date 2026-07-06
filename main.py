#!/usr/bin/env python3

"""main.py
Single-file AWS SOP Agent Console + ServiceNow approval-aware engine.
Keep only:  - main.py  - index.html  - requirements.txt  - .env  - start.sh
Core workflow:
  1. Admin creates AWS-SOP-Agent-* catalog from this console.
  2. Admin optionally selects approval required + fixed approver from active sys_user list.
  3. Requester raises a ServiceNow catalog request.
  4. Requester does NOT choose approver.
  5. Engine checks catalog metadata.
  6. If approval required:
       - create sysapproval_approver record
       - set RITM approval=requested
       - do not dispatch to AWS Agent
  7. Once approved:
       - engine dispatches once to AWS DevOps Agent
  8. If approval not required:
       - engine dispatches directly
  9. AWS DevOps Agent performs action and updates/closes RITM.

Environment variables:
  SNOW_INSTANCE
  SNOW_USER
  SNOW_PASSWORD
  AWS_AGENT_WEBHOOK_URL
  AWS_AGENT_HMAC_KEY

Optional:
  APP_PORT
  URL_PREFIX
  SOP_AGENT_PREFIX
  OPEN_STATE_QUERY
  AWS_ACCOUNT_ID
  AWS_REGION
  AWS_SERVICE
  CATALOG_TITLE
"""

import os
import re
import json
import time
import hmac
import base64
import hashlib
import logging
import datetime
import threading
import html
from collections import deque

import requests
from flask import Flask, Blueprint, request, jsonify, render_template

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass


# =============================================================================
# CONFIG
# =============================================================================

HERE = os.path.abspath(os.path.dirname(__file__))

APP_PORT = int(os.getenv("APP_PORT", "8000"))
URL_PREFIX = os.getenv("URL_PREFIX", "").rstrip("/")

SOP_AGENT_PREFIX = os.getenv("SOP_AGENT_PREFIX", "AWS-SOP-Agent-")
CATALOG_TITLE = os.getenv("CATALOG_TITLE", "Service Catalog")

AWS_AGENT_WEBHOOK_URL = os.getenv("AWS_AGENT_WEBHOOK_URL", "").strip()
AWS_AGENT_HMAC_KEY = os.getenv("AWS_AGENT_HMAC_KEY", "")
SNOW_APPROVAL_API_URL = os.getenv("SNOW_APPROVAL_API_URL","").strip()

AWS_ACCOUNT_ID = os.getenv("AWS_ACCOUNT_ID", "")
AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
AWS_SERVICE = os.getenv("AWS_SERVICE", "AWSPowerManagement")

OPEN_STATE_QUERY = os.getenv("OPEN_STATE_QUERY", "state=1")

STATE_WORK_IN_PROGRESS = "2"
STATE_CLOSED_COMPLETE = "3"
STATE_CLOSED_INCOMPLETE = "4"
STATE_CLOSED_SKIPPED = "7"

CLOSED_STATES = [STATE_CLOSED_COMPLETE, STATE_CLOSED_INCOMPLETE, STATE_CLOSED_SKIPPED]

REDISPATCH_MARKER = "SOP-ENGINE-DISPATCHED-AFTER-APPROVAL"
DIRECT_DISPATCH_MARKER = "SOP-ENGINE-DIRECT-DISPATCHED"
APPROVAL_CREATED_MARKER = "SOP-ENGINE-APPROVAL-CREATED"

TYPE_TEXT = "6"
TYPE_SELECT_BOX = "5"

RITM_VAR_LIMIT = 50

META_MARKER = "AWS_SOP_META="
_CHOICES_TABLE = None
_CHOICES_FALLBACKS = ["question_choices", "question_choice", "sc_item_option_choice"]


# =============================================================================
# LOGGING
# =============================================================================

LOG_BUFFER = deque(maxlen=1000)


class BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:
            pass


_fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S")

_buf_handler = BufferHandler()
_buf_handler.setFormatter(_fmt)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_fmt)

log = logging.getLogger("sop-engine")
log.setLevel(logging.INFO)

if not log.handlers:
    log.addHandler(_buf_handler)
    log.addHandler(_console_handler)


# =============================================================================
# SERVICENOW CLIENT
# =============================================================================

def normalize_instance(value):
    value = (value or "").strip()
    value = value.replace("https://", "").replace("http://", "")
    return value.rstrip("/")


def snow_cfg():
    return (
        normalize_instance(os.getenv("SNOW_INSTANCE", "")),
        os.getenv("SNOW_USER", ""),
        os.getenv("SNOW_PASSWORD", ""),
    )


def snow_missing_config():
    instance, user, password = snow_cfg()
    missing = []

    if not instance:
        missing.append("SNOW_INSTANCE")
    if not user:
        missing.append("SNOW_USER")
    if not password:
        missing.append("SNOW_PASSWORD")

    return missing


def snow_base():
    instance, _, _ = snow_cfg()
    return f"https://{instance}/api/now"


def snow_auth():
    _, user, password = snow_cfg()
    return user, password


def snow_instance_host():
    instance, _, _ = snow_cfg()
    return instance


SNOW_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def snow_check(response):
    if response.status_code < 400:
        return response

    detail = ""

    try:
        body = response.json()
        err = body.get("error") or {}
        detail = err.get("message") or err.get("detail") or json.dumps(body)
    except Exception:
        detail = (response.text or "")[:500]

    raise requests.HTTPError(
        f"HTTP {response.status_code} on "
        f"{response.request.method} {response.url} -> {detail}"
    )


def snow_get(table, query, fields=None, limit=100, display_value=None):
    params = {
        "sysparm_query": query,
        "sysparm_limit": limit,
    }

    if fields:
        params["sysparm_fields"] = ",".join(fields)

    if display_value:
        params["sysparm_display_value"] = display_value

    response = requests.get(
        f"{snow_base()}/table/{table}",
        auth=snow_auth(),
        headers=SNOW_HEADERS,
        params=params,
        timeout=30,
    )

    snow_check(response)
    return response.json().get("result", [])


def snow_create(table, body):
    response = requests.post(
        f"{snow_base()}/table/{table}",
        auth=snow_auth(),
        headers=SNOW_HEADERS,
        data=json.dumps(body),
        timeout=30,
    )

    snow_check(response)
    return response.json()["result"]


def snow_update(table, sys_id, body):
    response = requests.patch(
        f"{snow_base()}/table/{table}/{sys_id}",
        auth=snow_auth(),
        headers=SNOW_HEADERS,
        data=json.dumps(body),
        timeout=30,
    )

    snow_check(response)
    return response.json()["result"]


def create_approval_via_scripted_rest(
    approver_sys_id,
    ritm_sys_id
):

    if not SNOW_APPROVAL_API_URL:
        raise ValueError(
            "SNOW_APPROVAL_API_URL is not configured"
        )

    payload = {
        "approver": approver_sys_id,
        "sysapproval": ritm_sys_id
    }

    response = requests.post(
        SNOW_APPROVAL_API_URL,
        auth=snow_auth(),
        headers={
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    print("\n========== SCRIPTED REST RAW RESPONSE ==========")
    print(json.dumps(data, indent=2))
    print("================================================\n")

    return data["result"]

def snow_delete(table, sys_id):
    response = requests.delete(
        f"{snow_base()}/table/{table}/{sys_id}",
        auth=snow_auth(),
        headers=SNOW_HEADERS,
        timeout=30,
    )

    if response.status_code not in (200, 204):
        snow_check(response)

    return True


def snow_attach(table, sys_id, file_name, content_bytes, content_type="text/markdown"):
    url = f"{snow_base()}/attachment/file"

    params = {
        "table_name": table,
        "table_sys_id": sys_id,
        "file_name": file_name,
    }

    response = requests.post(
        url,
        auth=snow_auth(),
        params=params,
        headers={
            "Content-Type": content_type,
            "Accept": "application/json",
        },
        data=content_bytes,
        timeout=30,
    )

    snow_check(response)
    return response.json().get("result", {})


def unwrap(value):
    if isinstance(value, dict):
        return value.get("value", value.get("display_value", ""))
    return value


def display(value):
    if isinstance(value, dict):
        return value.get("display_value", value.get("value", ""))
    return value


def safe_call(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


# =============================================================================
# METADATA HELPERS
# =============================================================================

def encode_catalog_description(sop_html, meta):
    """
    ServiceNow strips HTML comments.
    Store metadata as plain text.
    """

    meta_json = json.dumps(
        meta or {},
        separators=(",", ":")
    )

    return (
        (sop_html or "")
        + "\n\n"
        + META_MARKER
        + meta_json
    )


def extract_catalog_meta(description):

    description = html.unescape(description or "")

    default_meta = {
        "approval_required": False,
        "approver_sys_id": "",
        "approver_name": "",
    }

    if META_MARKER not in description:
        return default_meta, description

    try:
        idx = description.rfind(META_MARKER)

        raw_json = description[
            idx + len(META_MARKER):
        ].strip()

        meta = json.loads(raw_json)

        sop = description[:idx].strip()

        default_meta.update(meta)

        return default_meta, sop

    except Exception as exc:
        print("META PARSE ERROR:", exc)
        return default_meta, description



def strip_catalog_meta(description):
    _, sop = extract_catalog_meta(description)
    return sop


# =============================================================================
# USER LOOKUP
# =============================================================================

def search_users(term="", limit=200):
    term = (term or "").strip()

    if term:
        query = f"active=true^nameLIKE{term}^ORemailLIKE{term}^ORDERBYname"
    else:
        query = "active=true^ORDERBYname"

    rows = snow_get(
        "sys_user",
        query,
        fields=["sys_id", "name", "email", "user_name"],
        limit=limit,
    )

    users = []

    for row in rows:
        name = row.get("name") or row.get("user_name") or row.get("email") or row.get("sys_id")
        email = row.get("email") or ""

        users.append({
            "sys_id": row.get("sys_id"),
            "name": name,
            "email": email,
            "label": f"{name} ({email})" if email else name,
        })

    return users


# =============================================================================
# CATALOG MANAGEMENT
# =============================================================================

def slug(name):
    text = re.sub(r"[^a-zA-Z0-9]+", "_", (name or "").strip())
    text = text.strip("_").lower()
    return text or "field"


def full_catalog_name(short_name):
    short_name = (short_name or "").strip()

    if short_name.startswith(SOP_AGENT_PREFIX):
        return short_name

    return f"{SOP_AGENT_PREFIX}{short_name}"


def choices_table():
    global _CHOICES_TABLE

    if _CHOICES_TABLE:
        return _CHOICES_TABLE

    candidates = []

    try:
        rows = snow_get("sys_db_object", "nameLIKEchoice", ["name"], 100)
        candidates = [row["name"] for row in rows if row.get("name")]
    except Exception:
        candidates = []

    for name in candidates:
        try:
            rows = snow_get(
                "sys_dictionary",
                f"name={name}^element=question",
                ["element"],
                1,
            )
            if rows:
                _CHOICES_TABLE = name
                return name
        except Exception:
            pass

    for fallback in _CHOICES_FALLBACKS:
        if fallback in candidates:
            _CHOICES_TABLE = fallback
            return fallback

    _CHOICES_TABLE = candidates[0] if candidates else "question_choices"
    return _CHOICES_TABLE


def resolve_catalog_and_category(category_title):
    catalog = snow_get(
        "sc_catalog",
        f"title={CATALOG_TITLE}",
        ["sys_id"],
        1,
    )

    catalog_id = catalog[0]["sys_id"] if catalog else ""

    category_query = f"title={category_title}"

    if catalog_id:
        category_query += f"^sc_catalog={catalog_id}"

    existing = snow_get(
        "sc_category",
        category_query,
        ["sys_id"],
        1,
    )

    if existing:
        return catalog_id, existing[0]["sys_id"]

    body = {
        "title": category_title,
        "active": "true",
    }

    if catalog_id:
        body["sc_catalog"] = catalog_id

    category_id = snow_create("sc_category", body)["sys_id"]
    return catalog_id, category_id


def create_readonly_client_script(item_id, item_name, variable_names):
    """
    Best-effort ServiceNow Catalog Client Script.

    Goal:
      - variables should remain editable while ordering from catalog page
      - variables should be read-only on Requested Item / Catalog Task view

    ServiceNow field names differ by version/PDI, so this is intentionally
    best-effort and will not block catalog creation if script creation fails.
    """
    if not variable_names:
        return None

    js_names = json.dumps(variable_names)

    script = f"""
function onLoad() {{
  var vars = {js_names};
  for (var i = 0; i < vars.length; i++) {{
    try {{
      g_form.setReadOnly(vars[i], true);
    }} catch (e) {{}}
  }}
}}
""".strip()

    candidate_bodies = [
        {
            "cat_item": item_id,
            "name": f"{item_name} - lock variables on RITM",
            "type": "onLoad",
            "script": script,
            "active": "true",
            "applies_catalog": "false",
            "applies_req_item": "true",
            "applies_sc_task": "true",
        },
        {
            "cat_item": item_id,
            "name": f"{item_name} - lock variables on RITM",
            "type": "onLoad",
            "script": script,
            "active": "true",
            "applies_to": "requested_item",
        },
        {
            "cat_item": item_id,
            "name": f"{item_name} - lock variables on RITM",
            "type": "onLoad",
            "script": script,
            "active": "true",
        },
    ]

    last_error = None

    for body in candidate_bodies:
        try:
            return snow_create("catalog_script_client", body)
        except Exception as exc:
            last_error = exc

    raise last_error


def list_catalogs():
    rows = snow_get(
        "sc_cat_item",
        f"nameSTARTSWITH{SOP_AGENT_PREFIX}",
        fields=["sys_id", "name", "short_description", "active", "description"],
        limit=1000,
    )

    output = []

    for row in rows:
        variable_rows = snow_get(
            "item_option_new",
            f"cat_item={row['sys_id']}",
            fields=["name", "type", "question_text"],
            limit=100,
        )

        meta, sop = extract_catalog_meta(row.get("description", ""))

        output.append({
            "sys_id": row["sys_id"],
            "name": row["name"],
            "short_description": row.get("short_description", ""),
            "active": row.get("active", "true"),
            "approval_required": bool(meta.get("approval_required")),
            "approver_sys_id": meta.get("approver_sys_id", ""),
            "approver_name": meta.get("approver_name", ""),
            "fields": [var.get("name") for var in variable_rows],
        })

    output.sort(key=lambda item: item["name"])
    return output


def create_catalog(
    short_name,
    short_description,
    sop_markdown,
    fields,
    approval_required=False,
    approver_sys_id="",
    approver_name="",
):
    name = full_catalog_name(short_name)

    existing = snow_get(
        "sc_cat_item",
        f"name={name}",
        fields=["sys_id"],
        limit=1,
    )

    if existing:
        raise ValueError(
            f"A catalog named '{name}' already exists. "
            f"Delete it first because edit is not enabled."
        )

    if approval_required and not approver_sys_id:
        raise ValueError("Approver is required when Approval Required is enabled.")

    catalog_id, category_id = resolve_catalog_and_category(name)

    meta = {
        "approval_required": bool(approval_required),
        "approver_sys_id": approver_sys_id or "",
        "approver_name": approver_name or "",
    }

    item_body = {
        "name": name,
        "short_description": short_description or name,
        "description": encode_catalog_description(sop_markdown or "", meta),
        "active": "true",
        "billable": "false",
    }

    print("\n========== METADATA SAVED ==========")
    print(json.dumps(meta, indent=2))
    print("====================================\n")

    if category_id:
        item_body["category"] = category_id

    if catalog_id:
        item_body["sc_catalogs"] = catalog_id

    item_id = snow_create("sc_cat_item", item_body)["sys_id"]

    warnings = []
    order = 100
    created_variable_names = []

    for field in fields:
        label = (field.get("label") or "").strip()

        if not label:
            continue

        field_type = field.get("type")
        is_dropdown = field_type == "dropdown"

        if is_dropdown:
            type_code = TYPE_SELECT_BOX
        else:
            type_code = TYPE_TEXT

        var_name = slug(field.get("name") or label)

        created_variable_names.append(var_name)

        var_body = {
            "cat_item": item_id,
            "name": var_name,
            "question_text": label,
            "type": type_code,
            "mandatory": "true" if field.get("mandatory", True) else "false",
            "active": "true",
            "order": str(order),
        }

        var_id = snow_create("item_option_new", var_body)["sys_id"]

        if is_dropdown:
            choice_order = 100
            created = 0

            for choice in field.get("choices", []):
                choice_label = (choice.get("label") or "").strip()

                if not choice_label:
                    continue

                choice_value = (choice.get("value") or slug(choice_label)).strip()

                try:
                    snow_create(
                        choices_table(),
                        {
                            "question": var_id,
                            "text": choice_label,
                            "value": choice_value,
                            "order": str(choice_order),
                            "inactive": "false",
                        },
                    )

                    created += 1
                    choice_order += 100

                except Exception as exc:
                    warnings.append(
                        f"choice '{choice_label}' on '{label}': {exc}"
                    )

            if created == 0:
                warnings.append(
                    f"dropdown '{label}' was created with no options."
                )

        order += 100

    if sop_markdown:
        try:
            snow_attach(
                "sc_cat_item",
                item_id,
                f"{name}-SOP.md",
                sop_markdown.encode("utf-8"),
            )
        except Exception as exc:
            warnings.append(
                f"SOP attachment failed. It is still saved in description: {exc}"
            )

    try:
        create_readonly_client_script(item_id, name, created_variable_names)
    except Exception as exc:
        warnings.append(
            "Could not create read-only Catalog Client Script automatically. "
            f"You may need to lock variables manually in ServiceNow. Error: {exc}"
        )

    result = {
        "sys_id": item_id,
        "name": name,
    }

    if warnings:
        result["warnings"] = warnings

    return result


def read_ritm_vars(ritm_sys_id):
    rows = safe_call(
        snow_get,
        "sc_item_option_mtom",
        f"request_item={ritm_sys_id}",
        ["sc_item_option.item_option_new.name", "sc_item_option.value"],
        100,
        "all",
    ) or []

    output = {}

    for row in rows:
        name = unwrap(row.get("sc_item_option.item_option_new.name"))
        value = unwrap(row.get("sc_item_option.value"))

        if name:
            output[name] = value

    return output


def list_orders(cat_item_sys_id=None, limit=RITM_VAR_LIMIT):
    if cat_item_sys_id:
        items = snow_get(
            "sc_cat_item",
            f"sys_id={cat_item_sys_id}",
            ["sys_id", "name"],
            1,
        )
    else:
        items = snow_get(
            "sc_cat_item",
            f"nameSTARTSWITH{SOP_AGENT_PREFIX}",
            ["sys_id", "name"],
            1000,
        )

    id_to_name = {
        item["sys_id"]: item["name"]
        for item in items
    }

    if not id_to_name:
        return []

    ids = ",".join(id_to_name.keys())

    rows = snow_get(
        "sc_req_item",
        f"cat_itemIN{ids}^ORDERBYDESCsys_created_on",
        ["sys_id", "number", "state", "approval", "cat_item", "sys_created_on"],
        limit,
        display_value="all",
    )

    output = []

    for row in rows:
        ritm_id = unwrap(row.get("sys_id"))

        output.append({
            "number": unwrap(row.get("number")),
            "catalog": id_to_name.get(unwrap(row.get("cat_item")), "-"),
            "state": display(row.get("state")),
            "approval": display(row.get("approval")),
            "created": unwrap(row.get("sys_created_on")),
            "fields": read_ritm_vars(ritm_id),
        })

    return output


def delete_catalog(sys_id):
    variables = safe_call(
        snow_get,
        "item_option_new",
        f"cat_item={sys_id}",
        ["sys_id"],
        200,
    ) or []

    for variable in variables:
        choices = safe_call(
            snow_get,
            choices_table(),
            f"question={variable['sys_id']}",
            ["sys_id"],
            200,
        ) or []

        for choice in choices:
            safe_call(snow_delete, choices_table(), choice["sys_id"])

        safe_call(snow_delete, "item_option_new", variable["sys_id"])

    attachments = safe_call(
        snow_get,
        "sys_attachment",
        f"table_name=sc_cat_item^table_sys_id={sys_id}",
        ["sys_id"],
        200,
    ) or []

    for attachment in attachments:
        safe_call(snow_delete, "sys_attachment", attachment["sys_id"])

    scripts = safe_call(
        snow_get,
        "catalog_script_client",
        f"cat_item={sys_id}",
        ["sys_id"],
        200,
    ) or []

    for script in scripts:
        safe_call(snow_delete, "catalog_script_client", script["sys_id"])

    snow_delete("sc_cat_item", sys_id)
    return True


# =============================================================================
# APPROVAL HELPERS
# =============================================================================

def approval_exists(ritm_sys_id):
    rows = safe_call(
        snow_get,
        "sysapproval_approver",
        f"sysapproval={ritm_sys_id}",
        ["sys_id", "state", "approver"],
        10,
        "all",
    ) or []

    return rows


def create_approval_for_ritm(
    ritm_sys_id,
    number,
    approver_sys_id,
    approver_name,
    item_name
):
    existing = approval_exists(ritm_sys_id)

    if existing:
        log.info(
            "[%s] approval already exists, skipping approval creation",
            number
        )
        return existing[0]

    debug_payload = {
        "approver": approver_sys_id,
        "sysapproval": ritm_sys_id
    }

    print("\n========== APPROVAL REQUEST ==========")
    print(json.dumps(debug_payload, indent=2))
    print("======================================\n")

    approval = create_approval_via_scripted_rest(
        approver_sys_id=approver_sys_id,
        ritm_sys_id=ritm_sys_id
    )

    print("\n========== APPROVAL RESPONSE ==========")
    print(json.dumps(approval, indent=2))
    print("=======================================\n")

    update_body = {
        "approval": "requested",
        "work_notes": (
            f"SOP engine: approval requested from "
            f"{approver_name or approver_sys_id}.\n"
            f"Catalog: {item_name}\n"
            f"{APPROVAL_CREATED_MARKER}"
        ),
    }

    safe_call(
        snow_update,
        "sc_req_item",
        ritm_sys_id,
        update_body
    )

    log.info(
        "[%s] approval requested from %s",
        number,
        approver_name or approver_sys_id
    )

    return approval


def approval_was_created_by_engine(ritm_sys_id):
    rows = safe_call(
        snow_get,
        "sys_journal_field",
        f"element_id={ritm_sys_id}^element=work_notes^valueLIKE{APPROVAL_CREATED_MARKER}",
        ["sys_id"],
        1,
    ) or []

    return bool(rows)


def already_dispatched_after_approval(ritm_sys_id):
    rows = safe_call(
        snow_get,
        "sys_journal_field",
        f"element_id={ritm_sys_id}^element=work_notes^valueLIKE{REDISPATCH_MARKER}",
        ["sys_id"],
        1,
    ) or []

    return bool(rows)


def already_direct_dispatched(ritm_sys_id):
    rows = safe_call(
        snow_get,
        "sys_journal_field",
        f"element_id={ritm_sys_id}^element=work_notes^valueLIKE{DIRECT_DISPATCH_MARKER}",
        ["sys_id"],
        1,
    ) or []

    return bool(rows)


# =============================================================================
# ENGINE
# =============================================================================

def engine_missing_config():
    missing = snow_missing_config()

    if not AWS_AGENT_WEBHOOK_URL:
        missing.append("AWS_AGENT_WEBHOOK_URL")

    if not AWS_AGENT_HMAC_KEY:
        missing.append("AWS_AGENT_HMAC_KEY")

    return missing


def resolve_cat_items():
    rows = snow_get(
        "sc_cat_item",
        f"nameSTARTSWITH{SOP_AGENT_PREFIX}^active=true",
        fields=["sys_id", "name", "short_description", "description"],
        limit=1000,
    )

    result = {}

    for row in rows:
        meta, sop = extract_catalog_meta(row.get("description", ""))

        print("\n======================")
        print("CATALOG:", row.get("name"))
        print("META:", meta)
        print("======================\n")

        row["_meta"] = meta
        row["_sop"] = sop

        result[row["sys_id"]] = row

    return result


def get_candidate_orders(item_ids):
    if not item_ids:
        return []

    ids = ",".join(item_ids)
    closed = ",".join(CLOSED_STATES)

    fresh = f"cat_itemIN{ids}^{OPEN_STATE_QUERY}"
    approved = f"cat_itemIN{ids}^approval=approved^stateNOT IN{closed}"
    rejected = f"cat_itemIN{ids}^approval=rejected^stateNOT IN{closed}"

    query = f"{fresh}^NQ{approved}^NQ{rejected}"

    return snow_get(
        "sc_req_item",
        query,
        fields=[
            "sys_id",
            "number",
            "state",
            "approval",
            "request.number",
            "short_description",
            "cat_item",
        ],
        display_value="all",
    )


def get_order_variables(ritm_sys_id):
    rows = snow_get(
        "sc_item_option_mtom",
        f"request_item={ritm_sys_id}",
        fields=[
            "sc_item_option.item_option_new.name",
            "sc_item_option.value",
        ],
        display_value="all",
    )

    variables = {}

    for row in rows:
        name = unwrap(row.get("sc_item_option.item_option_new.name"))
        value = unwrap(row.get("sc_item_option.value"))

        if name:
            variables[name] = value

    return variables


def sign_payload(timestamp, body_string):
    context = f"{timestamp}:{body_string}"

    digest = hmac.new(
        AWS_AGENT_HMAC_KEY.encode("utf-8"),
        context.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    return base64.b64encode(digest).decode("utf-8")


def dispatch_to_agent(payload, order_number):
    timestamp = datetime.datetime.now(
        datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    body_string = json.dumps(payload, separators=(",", ":"))
    signature = sign_payload(timestamp, body_string)

    headers = {
        "Content-Type": "application/json",
        "x-amzn-event-timestamp": timestamp,
        "x-amzn-event-signature": signature,
        "x-idempotency-key": order_number,
    }

    log.info("[%s] POST %s", order_number, AWS_AGENT_WEBHOOK_URL)
    log.info(
        "[%s] headers: %s",
        order_number,
        {
            **headers,
            "x-amzn-event-signature": signature[:12] + "...(truncated)",
        },
    )
    log.info("[%s] body: %s", order_number, body_string)

    response = requests.post(
        AWS_AGENT_WEBHOOK_URL,
        data=body_string.encode("utf-8"),
        headers=headers,
        timeout=60,
    )

    response.raise_for_status()
    return response


def build_payload(ritm, variables, item_name, sop_text, approval_status):
    raw_ids = variables.get("instance_ids", "")
    instance_ids = [
        item.strip()
        for item in raw_ids.split(",")
        if item.strip()
    ]

    order_number = unwrap(ritm.get("number"))
    ritm_sys_id = unwrap(ritm.get("sys_id"))
    action = variables.get("action", "")

    now = datetime.datetime.now(
        datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    unique_incident_id = f"{order_number}-{int(time.time())}"

    host = snow_instance_host()

    snow_url = (
        f"https://{host}/sc_req_item.do?sys_id={ritm_sys_id}"
        if host else ""
    )

    return {
        "eventType": "incident",
        "incidentId": unique_incident_id,
        "action": "created",
        "priority": "HIGH",
        "title": (
            f"AWS Power Management: {action} "
            f"{', '.join(instance_ids) or '(no instances)'}"
        ),
        "description": (
            f"Catalog order {order_number}: action '{action}' "
            f"on {', '.join(instance_ids)}."
        ),
        "service": AWS_SERVICE,
        "timestamp": now,
        "data": {
            "alertName": item_name,
            "awsAccountId": AWS_ACCOUNT_ID,
            "awsRegion": AWS_REGION,
            "requestedAction": action,
            "instanceIds": instance_ids,
            "submittedFields": variables,
            "approvalStatus": approval_status,
            "approvalGranted": approval_status == "approved",
            "sop": {
                "name": item_name,
                "content": sop_text,
            },
            "serviceNowUrl": snow_url,
            "realServiceNowTicket": order_number,
            "incidentSysId": ritm_sys_id,
            "callback": {
                "table": "sc_req_item",
                "sys_id": ritm_sys_id,
            },
        },
    }


def dispatch_order(ritm, item_name, sop_text, approval_status, marker):
    number = unwrap(ritm.get("number"))
    ritm_id = unwrap(ritm.get("sys_id"))

    variables = get_order_variables(ritm_id)

    payload = build_payload(
        ritm=ritm,
        variables=variables,
        item_name=item_name,
        sop_text=sop_text,
        approval_status=approval_status,
    )

    log.info(
        "[%s] (%s) dispatching. approval=%s variables=%s",
        number,
        item_name,
        approval_status,
        variables,
    )

    try:
        response = dispatch_to_agent(payload, number)

    except requests.RequestException as exc:
        log.error("[%s] dispatch failed: %s", number, exc)

        snow_update(
            "sc_req_item",
            ritm_id,
            {
                "work_notes": (
                    "SOP engine: dispatch to AWS DevOps Agent FAILED "
                    f"({exc}). Will retry."
                )
            },
        )

        return

    response_body = (response.text or "")[:800]

    log.info(
        "[%s] agent responded: HTTP %s | body: %s",
        number,
        response.status_code,
        response_body,
    )

    note = (
        f"SOP engine: dispatched to AWS DevOps Agent.\n"
        f"Approval status: {approval_status}\n"
        f"Catalog: {item_name}\n"
        f"Submitted: {variables}\n"
        f"Agent HTTP status: {response.status_code}\n"
        f"Agent response: {(response.text or '')[:400]}\n"
        f"{marker}"
    )

    snow_update(
        "sc_req_item",
        ritm_id,
        {
            "state": STATE_WORK_IN_PROGRESS,
            "work_notes": note,
        },
    )

    log.info("[%s] dispatched -> Work in Progress", number)


def get_approval_record_state(ritm_sys_id):

    try:
        rows = snow_get(
            "sysapproval_approver",
            f"sysapproval={ritm_sys_id}",
            ["state"],
            1,
            "all"
        )

        if not rows:
            return ""

        return (
            unwrap(rows[0].get("state")) or ""
        ).lower()

    except Exception as exc:
        log.warning(
            "Unable to read approval state for %s: %s",
            ritm_sys_id,
            exc
        )
        return ""

def process_order(ritm, item):

    number = unwrap(ritm.get("number"))
    ritm_id = unwrap(ritm.get("sys_id"))

    item_name = item.get("name", "UNKNOWN")
    sop_text = item.get("_sop", "")
    meta = item.get("_meta", {}) or {}

    approval_required = bool(meta.get("approval_required"))
    approver_sys_id = meta.get("approver_sys_id", "")
    approver_name = meta.get("approver_name", "")

    approval = (
        unwrap(ritm.get("approval")) or ""
    ).lower() or "not requested"

    approval_record_state = get_approval_record_state(
        ritm_id
    )

    print("\n========== APPROVAL DEBUG ==========")
    print("RITM:", number)
    print("ritm approval:", approval)
    print("approval record:", approval_record_state)
    print("====================================\n")

    #
    # REJECTED
    #
    if (
        approval == "rejected"
        or approval_record_state == "rejected"
    ):
        log.info(
            "[%s] approval rejected -> closing incomplete",
            number
        )

        snow_update(
            "sc_req_item",
            ritm_id,
            {
                "state": STATE_CLOSED_INCOMPLETE,
                "work_notes": (
                    "SOP engine: approval was rejected. "
                    "Closing as incomplete."
                ),
            },
        )

        return

    #
    # APPROVAL REQUIRED
    #
    if approval_required:

        #
        # APPROVED
        #
        if (
            approval == "approved"
            or approval_record_state == "approved"
        ):

            if already_dispatched_after_approval(
                ritm_id
            ):
                log.info(
                    "[%s] already dispatched after approval -> skipping",
                    number
                )
                return

            log.info(
                "[%s] approval granted -> dispatching",
                number
            )

            dispatch_order(
                ritm=ritm,
                item_name=item_name,
                sop_text=sop_text,
                approval_status="approved",
                marker=REDISPATCH_MARKER,
            )

            return

        #
        # WAITING FOR APPROVAL
        #
        if (
            approval in ("requested", "not requested", "")
            or approval_record_state == "requested"
        ):

            if not approver_sys_id:

                log.warning(
                    "[%s] approval required but no approver configured",
                    number
                )

                snow_update(
                    "sc_req_item",
                    ritm_id,
                    {
                        "work_notes": (
                            "SOP engine: approval is required, "
                            "but no approver is configured "
                            "on the catalog."
                        )
                    },
                )

                return

            if not approval_was_created_by_engine(
                ritm_id
            ):

                create_approval_for_ritm(
                    ritm_sys_id=ritm_id,
                    number=number,
                    approver_sys_id=approver_sys_id,
                    approver_name=approver_name,
                    item_name=item_name,
                )

            else:

                log.info(
                    "[%s] waiting for approval from %s",
                    number,
                    approver_name or approver_sys_id
                )

            return

        log.info(
            "[%s] approval required. ritm=%s approval_record=%s",
            number,
            approval,
            approval_record_state
        )

        return

    #
    # NO APPROVAL REQUIRED
    #
    if already_direct_dispatched(
        ritm_id
    ):

        log.info(
            "[%s] directly dispatched already -> skipping",
            number
        )

        return

    dispatch_order(
        ritm=ritm,
        item_name=item_name,
        sop_text=sop_text,
        approval_status="not required",
        marker=DIRECT_DISPATCH_MARKER,
    )

def poll_once():
    item_map = resolve_cat_items()

    if not item_map:
        log.warning(
            "No catalog items found with prefix '%s'.",
            SOP_AGENT_PREFIX,
        )
        return

    log.info(
        "Watching %d catalog(s): %s",
        len(item_map),
        ", ".join(sorted(item["name"] for item in item_map.values())),
    )

    orders = get_candidate_orders(list(item_map.keys()))

    log.info(
        "Found %d order(s) to consider. open + approved + rejected",
        len(orders),
    )

    for ritm in orders:
        try:
            item = item_map.get(unwrap(ritm.get("cat_item")), {})

            if not item:
                log.warning(
                    "No matching catalog item found for RITM %s",
                    unwrap(ritm.get("number")),
                )
                continue

            process_order(ritm, item)

        except Exception as exc:
            log.exception(
                "Error processing %s: %s",
                unwrap(ritm.get("number")),
                exc,
            )


def run_loop(stop_event, interval=30):
    log.info(
        "Engine loop started. interval=%ss prefix='%s'",
        interval,
        SOP_AGENT_PREFIX,
    )

    while not stop_event.is_set():
        try:
            poll_once()
        except Exception as exc:
            log.exception("Poll pass failed: %s", exc)

        stop_event.wait(interval)

    log.info("Engine loop stopped")


# =============================================================================
# FLASK APP
# =============================================================================

_engine_thread = None
_engine_stop = None
_engine_lock = threading.Lock()


def engine_running():
    return _engine_thread is not None and _engine_thread.is_alive()


app = Flask(__name__, template_folder=HERE)
app.url_map.strict_slashes = False

bp = Blueprint("sop", __name__)


@bp.route("/")
def index():
    return render_template(
        "index.html",
        prefix=SOP_AGENT_PREFIX,
        base=URL_PREFIX,
    )


@bp.route("/api/config")
def api_config():
    return jsonify({
        "prefix": SOP_AGENT_PREFIX,
        "base": URL_PREFIX,
        "missing_config": engine_missing_config(),
    })


@bp.route("/api/users")
def api_users():
    missing = snow_missing_config()

    if missing:
        return jsonify({
            "error": f"Missing config: {', '.join(missing)}"
        }), 400

    term = request.args.get("q", "")

    try:
        return jsonify({
            "users": search_users(term)
        })

    except Exception as exc:
        return jsonify({
            "error": str(exc)
        }), 502


@bp.route("/api/catalogs", methods=["GET"])
def api_list_catalogs():
    missing = snow_missing_config()

    if missing:
        return jsonify({
            "error": f"Missing config: {', '.join(missing)}"
        }), 400

    try:
        return jsonify({
            "catalogs": list_catalogs()
        })

    except Exception as exc:
        return jsonify({
            "error": str(exc)
        }), 502


@bp.route("/api/catalogs", methods=["POST"])
def api_create_catalog():
    missing = snow_missing_config()

    if missing:
        return jsonify({
            "error": f"Missing config: {', '.join(missing)}"
        }), 400

    data = request.get_json(force=True) or {}

    print("\n========== CATALOG REQUEST ==========")
    print(json.dumps(data, indent=2))
    print("=====================================\n")

    name = (data.get("name") or "").strip()

    if not name:
        return jsonify({
            "error": "Catalog name is required."
        }), 400

    fields = data.get("fields") or []

    if not fields:
        return jsonify({
            "error": "Add at least one order field."
        }), 400

    approval_required = bool(data.get("approval_required"))
    approver_sys_id = (data.get("approver_sys_id") or "").strip()
    approver_name = (data.get("approver_name") or "").strip()

    try:
        result = create_catalog(
            short_name=name,
            short_description=data.get("short_description", ""),
            sop_markdown=data.get("sop_markdown", ""),
            fields=fields,
            approval_required=approval_required,
            approver_sys_id=approver_sys_id,
            approver_name=approver_name,
        )

        return jsonify(result), 201

    except ValueError as exc:
        return jsonify({
            "error": str(exc)
        }), 409

    except Exception as exc:
        return jsonify({
            "error": str(exc)
        }), 502


@bp.route("/api/catalogs/<sys_id>", methods=["DELETE"])
def api_delete_catalog(sys_id):
    try:
        delete_catalog(sys_id)
        return jsonify({
            "deleted": sys_id
        })

    except Exception as exc:
        return jsonify({
            "error": str(exc)
        }), 502


@bp.route("/api/orders")
def api_list_orders():
    missing = snow_missing_config()

    if missing:
        return jsonify({
            "error": f"Missing config: {', '.join(missing)}"
        }), 400

    cat_item = request.args.get("cat_item") or None

    try:
        return jsonify({
            "orders": list_orders(cat_item)
        })

    except Exception as exc:
        return jsonify({
            "error": str(exc)
        }), 502


@bp.route("/api/engine/start", methods=["POST"])
def api_engine_start():
    global _engine_thread, _engine_stop

    missing = engine_missing_config()

    if missing:
        return jsonify({
            "error": f"Missing config: {', '.join(missing)}"
        }), 400

    with _engine_lock:
        if engine_running():
            return jsonify({
                "status": "running",
                "message": "Engine already running",
            })

        interval = int(
            (request.get_json(silent=True) or {}).get("interval", 30)
        )

        _engine_stop = threading.Event()

        _engine_thread = threading.Thread(
            target=run_loop,
            args=(_engine_stop, interval),
            daemon=True,
        )

        _engine_thread.start()

    return jsonify({
        "status": "running"
    })


@bp.route("/api/engine/stop", methods=["POST"])
def api_engine_stop():
    with _engine_lock:
        if _engine_stop:
            _engine_stop.set()

    return jsonify({
        "status": "stopping"
    })


@bp.route("/api/engine/status")
def api_engine_status():
    return jsonify({
        "running": engine_running()
    })


@bp.route("/api/engine/logs")
def api_engine_logs():
    since = int(request.args.get("since", 0))
    lines = list(LOG_BUFFER)

    return jsonify({
        "total": len(lines),
        "lines": lines[since:],
    })


@bp.route("/api/diag/choices")
def api_diag_choices():
    output = {}

    try:
        try:
            rows = snow_get(
                "sys_db_object",
                "nameLIKEchoice",
                ["name", "label"],
                100,
            )

            output["choice_tables"] = [
                {
                    "name": row.get("name"),
                    "label": row.get("label"),
                }
                for row in rows
            ]

        except Exception as exc:
            output["choice_tables_error"] = str(exc)

        try:
            output["detected_choices_table"] = choices_table()
        except Exception as exc:
            output["detect_error"] = str(exc)

    except Exception as exc:
        output["error"] = str(exc)

    return jsonify(output)


app.register_blueprint(bp, url_prefix=URL_PREFIX)


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    print(
        f" * AWS SOP Agent Console at path: "
        f"{URL_PREFIX or '/'}  health check: /healthz"
    )

    app.run(
        host="0.0.0.0",
        port=APP_PORT,
        debug=False,
        threaded=True,
    )
