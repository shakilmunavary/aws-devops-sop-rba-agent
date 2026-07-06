
#!/usr/bin/env python3
"""
engine_core.py

The polling engine logic, importable so the web UI can run it in a background
thread and stream its logs.

Approval-aware dispatch:
  * Brand-new orders (Open) are dispatched once. The agent reads each catalog's
    SOP (sent in data.sop.content) to decide whether the order needs approval
    (e.g. PROD-tagged instances) and, if so, creates a real ServiceNow approval
    routed to the group/user named in that SOP, then STOPS without acting.
  * The agent sets the request item's approval to "requested" -> the engine
    leaves it alone (no re-dispatch loop) until a human approves.
  * When approval is granted (approval=approved), the engine re-dispatches the
    order EXACTLY ONCE with data.approvalStatus = "approved", telling the agent
    to proceed even for PROD. A work-note marker prevents repeat dispatch.

All config comes from environment / .env. Nothing is hardcoded.
"""

import os
import json
import base64
import time
import hmac
import hashlib
import logging
import datetime
import threading

import requests

import snow_client as snow

log = logging.getLogger("sop-engine")

AWS_AGENT_WEBHOOK_URL = os.getenv("AWS_AGENT_WEBHOOK_URL", "").strip()
AWS_AGENT_HMAC_KEY = os.getenv("AWS_AGENT_HMAC_KEY", "")
SOP_AGENT_PREFIX = os.getenv("SOP_AGENT_PREFIX", "AWS-SOP-Agent-")
OPEN_STATE_QUERY = os.getenv("OPEN_STATE_QUERY", "state=1")  # fresh orders ("Open")

# Used to enrich the incident payload sent to the agent.
AWS_ACCOUNT_ID = os.getenv("AWS_ACCOUNT_ID", "")
AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
AWS_SERVICE = os.getenv("AWS_SERVICE", "AWSPowerManagement")
SNOW_INSTANCE_HOST = snow.normalize_instance(os.getenv("SNOW_INSTANCE", ""))

STATE_WORK_IN_PROGRESS = "2"
CLOSED_STATES = ["3", "4", "7"]   # Closed Complete / Incomplete / Skipped
REDISPATCH_MARKER = "SOP-ENGINE-REDISPATCHED-AFTER-APPROVAL"


def missing_config():
    missing = snow.missing_config()
    if not AWS_AGENT_WEBHOOK_URL:
        missing.append("AWS_AGENT_WEBHOOK_URL")
    if not AWS_AGENT_HMAC_KEY:
        missing.append("AWS_AGENT_HMAC_KEY")
    return missing


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #
def resolve_cat_items():
    rows = snow.get(
        "sc_cat_item",
        f"nameSTARTSWITH{SOP_AGENT_PREFIX}^active=true",
        fields=["sys_id", "name", "short_description", "description"],
        limit=1000,
    )
    return {row["sys_id"]: row for row in rows}


def get_open_orders(item_ids):
    """Two independent triggers, OR'd via ^NQ:
       1) brand-new orders that are Open (OPEN_STATE_QUERY)
       2) orders a human has APPROVED that are not yet closed (re-run)."""
    if not item_ids:
        return []
    ids = ",".join(item_ids)
    closed = ",".join(CLOSED_STATES)
    fresh = f"cat_itemIN{ids}^{OPEN_STATE_QUERY}"
    approved = f"cat_itemIN{ids}^approval=approved^stateNOT IN{closed}"
    query = f"{fresh}^NQ{approved}"
    return snow.get(
        "sc_req_item", query,
        fields=["sys_id", "number", "state", "approval",
                "request.number", "short_description", "cat_item"],
        display_value="all",
    )


def get_order_variables(ritm_sys_id):
    rows = snow.get(
        "sc_item_option_mtom",
        f"request_item={ritm_sys_id}",
        fields=["sc_item_option.item_option_new.name", "sc_item_option.value"],
        display_value="all",
    )
    variables = {}
    for row in rows:
        name = snow.unwrap(row.get("sc_item_option.item_option_new.name"))
        value = snow.unwrap(row.get("sc_item_option.value"))
        if name:
            variables[name] = value
    return variables


def _already_redispatched(ritm_sys_id):
    """True if the engine has already re-dispatched this order after approval."""
    try:
        rows = snow.get(
            "sys_journal_field",
            f"element_id={ritm_sys_id}^element=work_notes^valueLIKE{REDISPATCH_MARKER}",
            ["sys_id"], 1)
        return bool(rows)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def _sign(timestamp, body_str):
    """AWS DevOps Agent handshake: HMAC-SHA256 over "{timestamp}:{body}",
    base64-encoded (matches the agent's expected x-amzn-event-signature)."""
    context = f"{timestamp}:{body_str}"
    digest = hmac.new(AWS_AGENT_HMAC_KEY.encode("utf-8"), context.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def dispatch_to_agent(payload, order_number):
    # Same timestamp must be used in the signature AND the header.
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    body_str = json.dumps(payload, separators=(",", ":"))
    signature = _sign(timestamp, body_str)
    headers = {
        "Content-Type": "application/json",
        "x-amzn-event-timestamp": timestamp,
        "x-amzn-event-signature": signature,
        "x-idempotency-key": order_number,
    }
    # Full visibility into what we send.
    log.info("[%s] POST %s", order_number, AWS_AGENT_WEBHOOK_URL)
    log.info("[%s] headers: %s", order_number,
             {**headers, "x-amzn-event-signature": signature[:12] + "...(truncated)"})
    log.info("[%s] body: %s", order_number, body_str)
    r = requests.post(AWS_AGENT_WEBHOOK_URL, data=body_str.encode("utf-8"), headers=headers, timeout=60)
    r.raise_for_status()
    return r


def build_payload(ritm, variables, item_name, sop_text, approval_status):
    # instance_ids is the conventional field; fall back to any *_ids text field.
    raw_ids = variables.get("instance_ids", "")
    instance_ids = [s.strip() for s in raw_ids.split(",") if s.strip()]
    order_number = snow.unwrap(ritm.get("number"))
    ritm_sys_id = snow.unwrap(ritm.get("sys_id"))
    action = variables.get("action", "")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    unique_incident_id = f"{order_number}-{int(time.time())}"
    snow_url = f"https://{SNOW_INSTANCE_HOST}/sc_req_item.do?sys_id={ritm_sys_id}" if SNOW_INSTANCE_HOST else ""

    return {
        "eventType": "incident",
        "incidentId": unique_incident_id,
        "action": "created",
        "priority": "HIGH",
        "title": f"AWS Power Management: {action} {', '.join(instance_ids) or '(no instances)'}",
        "description": f"Catalog order {order_number}: action '{action}' on {', '.join(instance_ids)}.",
        "service": AWS_SERVICE,
        "timestamp": now,
        "data": {
            "alertName": item_name,
            "awsAccountId": AWS_ACCOUNT_ID,
            "awsRegion": AWS_REGION,
            "requestedAction": action,            # start | stop | restart
            "instanceIds": instance_ids,
            "submittedFields": variables,          # everything the user entered
            # Approval state for THIS dispatch:
            #   "approved"  -> a human has approved; agent should PROCEED (bypass guards)
            #   otherwise   -> agent applies the SOP's approval policy
            "approvalStatus": approval_status,
            "approvalGranted": approval_status == "approved",
            "sop": {"name": item_name, "content": sop_text},
            "serviceNowUrl": snow_url,
            "realServiceNowTicket": order_number,
            "incidentSysId": ritm_sys_id,
            "callback": {"table": "sc_req_item", "sys_id": ritm_sys_id},
        },
    }


# --------------------------------------------------------------------------- #
# Processing
# --------------------------------------------------------------------------- #
def process_order(ritm, item_name, sop_text):
    number = snow.unwrap(ritm.get("number"))
    ritm_id = snow.unwrap(ritm.get("sys_id"))
    approval = (snow.unwrap(ritm.get("approval")) or "").lower() or "not requested"
    is_rerun = approval == "approved"

    # Don't re-fire an approved order we've already re-dispatched (prevents loops
    # in the window before the agent closes the ticket).
    if is_rerun and _already_redispatched(ritm_id):
        log.info("[%s] approved & already re-dispatched -> skipping", number)
        return

    variables = get_order_variables(ritm_id)
    payload = build_payload(ritm, variables, item_name, sop_text, approval)
    log.info("[%s] (%s) dispatching (approval=%s): %s", number, item_name, approval, variables)

    try:
        resp = dispatch_to_agent(payload, number)
    except requests.RequestException as exc:
        log.error("[%s] dispatch failed: %s", number, exc)
        snow.update("sc_req_item", ritm_id,
                    {"work_notes": f"SOP engine: dispatch to AWS DevOps Agent FAILED ({exc}). Will retry."})
        return

    log.info("[%s] agent responded: HTTP %s | body: %s", number, resp.status_code, (resp.text or "")[:800])

    marker = f"\n{REDISPATCH_MARKER}" if is_rerun else ""
    verb = "RE-dispatched after approval" if is_rerun else "dispatched"
    note = (f"SOP engine: {verb} to AWS DevOps Agent.\n"
            f"Approval status: {approval}\n"
            f"Catalog: {item_name}\n"
            f"Submitted: {variables}\n"
            f"Agent HTTP status: {resp.status_code}\n"
            f"Agent response: {(resp.text or '')[:400]}{marker}")
    snow.update("sc_req_item", ritm_id, {"state": STATE_WORK_IN_PROGRESS, "work_notes": note})
    log.info("[%s] %s -> Work in Progress", number, verb)


def poll_once():
    item_map = resolve_cat_items()
    if not item_map:
        log.warning("No catalog items found with prefix '%s'.", SOP_AGENT_PREFIX)
        return
    log.info("Watching %d catalog(s): %s",
             len(item_map), ", ".join(sorted(i["name"] for i in item_map.values())))

    orders = get_open_orders(list(item_map.keys()))
    log.info("Found %d order(s) to consider (new + approved)", len(orders))

    for ritm in orders:
        try:
            item = item_map.get(snow.unwrap(ritm.get("cat_item")), {})
            process_order(ritm, item.get("name", "UNKNOWN"), item.get("description") or "")
        except Exception as exc:  # noqa: BLE001
            log.exception("Error processing %s: %s", snow.unwrap(ritm.get("number")), exc)


def run_loop(stop_event: threading.Event, interval: int = 30):
    log.info("Engine loop started (interval=%ss, prefix='%s')", interval, SOP_AGENT_PREFIX)
    while not stop_event.is_set():
        try:
            poll_once()
        except Exception as exc:  # noqa: BLE001
            log.exception("Poll pass failed: %s", exc)
        stop_event.wait(interval)
    log.info("Engine loop stopped")
