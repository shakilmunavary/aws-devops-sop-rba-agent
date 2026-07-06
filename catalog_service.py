#!/usr/bin/env python3
"""
catalog_service.py

Create / list / delete "AWS-SOP-Agent-*" Service Catalog items, driven by the UI.
There is intentionally no update path: to change a catalog you delete and recreate.

Supported field types from the UI:
  text     -> Single line text  (item_option_new.type = 6)
  dropdown -> Select box        (item_option_new.type = 5) with question_choices
"""

import os
import re

import snow_client as snow

SOP_AGENT_PREFIX = os.getenv("SOP_AGENT_PREFIX", "AWS-SOP-Agent-")
CATALOG_TITLE = os.getenv("CATALOG_TITLE", "Service Catalog")

TYPE_TEXT = "6"
TYPE_SELECT_BOX = "5"
TYPE_REFERENCE = "8"   # Reference variable (e.g. sys_user) - renders as a search box

# The table that stores Select Box choices has been named differently across
# ServiceNow versions (e.g. question_choices / question_choice). Rather than
# hardcode it, detect the table that has a "question" reference column.
_CHOICES_TABLE = None
_CHOICES_FALLBACKS = ["question_choices", "question_choice", "sc_item_option_choice"]


def choices_table():
    global _CHOICES_TABLE
    if _CHOICES_TABLE:
        return _CHOICES_TABLE
    # Look at every table whose name contains "choice" and pick the one whose
    # dictionary has an element named "question" (the link back to the variable).
    candidates = []
    try:
        rows = snow.get("sys_db_object", "nameLIKEchoice", ["name"], 100)
        candidates = [r["name"] for r in rows if r.get("name")]
    except Exception:
        candidates = []
    for name in candidates:
        try:
            if snow.get("sys_dictionary", f"name={name}^element=question", ["element"], 1):
                _CHOICES_TABLE = name
                return name
        except Exception:
            pass
    for pref in _CHOICES_FALLBACKS:
        if pref in candidates:
            _CHOICES_TABLE = pref
            return pref
    _CHOICES_TABLE = candidates[0] if candidates else "question_choices"
    return _CHOICES_TABLE


def _slug(name):
    """Turn a human field label into a valid ServiceNow variable name."""
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (name or "").strip()).strip("_").lower()
    return s or "field"


def full_name(short_name):
    short_name = (short_name or "").strip()
    if short_name.startswith(SOP_AGENT_PREFIX):
        return short_name
    return f"{SOP_AGENT_PREFIX}{short_name}"


def list_catalogs():
    rows = snow.get(
        "sc_cat_item",
        f"nameSTARTSWITH{SOP_AGENT_PREFIX}",
        fields=["sys_id", "name", "short_description", "active"],
        limit=1000,
    )
    out = []
    for r in rows:
        var_rows = snow.get("item_option_new", f"cat_item={r['sys_id']}",
                            fields=["name", "type"], limit=100)
        out.append({
            "sys_id": r["sys_id"],
            "name": r["name"],
            "short_description": r.get("short_description", ""),
            "active": r.get("active", "true"),
            "fields": [v.get("name") for v in var_rows],
        })
    out.sort(key=lambda x: x["name"])
    return out


def _resolve_catalog_and_category(category_title):
    catalog = snow.get("sc_catalog", f"title={CATALOG_TITLE}", fields=["sys_id"], limit=1)
    catalog_id = catalog[0]["sys_id"] if catalog else ""

    cat_query = f"title={category_title}"
    if catalog_id:
        cat_query += f"^sc_catalog={catalog_id}"
    existing = snow.get("sc_category", cat_query, fields=["sys_id"], limit=1)
    if existing:
        return catalog_id, existing[0]["sys_id"]

    body = {"title": category_title, "active": "true"}
    if catalog_id:
        body["sc_catalog"] = catalog_id
    category_id = snow.create("sc_category", body)["sys_id"]
    return catalog_id, category_id


def create_catalog(short_name, short_description, sop_markdown, fields):
    """fields: list of {label, type('text'|'dropdown'), mandatory(bool), choices[{label,value}]}"""
    name = full_name(short_name)

    if snow.get("sc_cat_item", f"name={name}", fields=["sys_id"], limit=1):
        raise ValueError(f"A catalog named '{name}' already exists. Delete it first (no edit).")

    catalog_id, category_id = _resolve_catalog_and_category(name)

    item_body = {
        "name": name,
        "short_description": short_description or name,
        "description": sop_markdown or "",   # SOP copied into description
        "active": "true",
        "billable": "false",
    }
    if category_id:
        item_body["category"] = category_id
    if catalog_id:
        item_body["sc_catalogs"] = catalog_id
    item_id = snow.create("sc_cat_item", item_body)["sys_id"]

    warnings = []
    order = 100
    for f in fields:
        label = (f.get("label") or "").strip()
        if not label:
            continue
        var_name = _slug(f.get("name") or label)
        ftype = f.get("type")
        is_dropdown = ftype == "dropdown"
        is_user = ftype in ("user", "reference")
        if is_dropdown:
            type_code = TYPE_SELECT_BOX
        elif is_user:
            type_code = TYPE_REFERENCE
        else:
            type_code = TYPE_TEXT
        var_body = {
            "cat_item": item_id,
            "name": var_name,
            "question_text": label,
            "type": type_code,
            "mandatory": "true" if f.get("mandatory", True) else "false",
            "active": "true",
            "order": str(order),
        }
        if is_user:
            # Reference to the users table -> type-ahead search of active users.
            var_body["reference"] = "sys_user"
            var_body["reference_qual"] = "active=true"
        var_id = snow.create("item_option_new", var_body)["sys_id"]

        if is_dropdown:
            corder = 100
            created = 0
            for choice in f.get("choices", []):
                ctext = (choice.get("label") or "").strip()
                if not ctext:
                    continue
                cvalue = (choice.get("value") or _slug(ctext)).strip()
                try:
                    snow.create(choices_table(), {
                        "question": var_id,
                        "text": ctext,
                        "value": cvalue,
                        "order": str(corder),
                        "inactive": "false",
                    })
                    created += 1
                    corder += 100
                except Exception as exc:  # noqa: BLE001 - report, don't abort the catalog
                    warnings.append(f"choice '{ctext}' on '{label}': {exc}")
            if created == 0:
                warnings.append(f"dropdown '{label}' was created with NO options "
                                f"(its choices could not be written).")
        order += 100

    # Also attach the SOP as a downloadable .md file (best-effort).
    if sop_markdown:
        try:
            snow.attach("sc_cat_item", item_id, f"{name}-SOP.md", sop_markdown.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"SOP attachment failed (it is still saved in the description): {exc}")

    result = {"sys_id": item_id, "name": name}
    if warnings:
        result["warnings"] = warnings
    return result


RITM_VAR_LIMIT = 50


def _read_ritm_vars(ritm_sys_id):
    rows = _safe(snow.get, "sc_item_option_mtom", f"request_item={ritm_sys_id}",
                 ["sc_item_option.item_option_new.name", "sc_item_option.value"], 100, "all") or []
    out = {}
    for row in rows:
        name = snow.unwrap(row.get("sc_item_option.item_option_new.name"))
        val = snow.unwrap(row.get("sc_item_option.value"))
        if name:
            out[name] = val
    return out


def list_orders(cat_item_sys_id=None, limit=RITM_VAR_LIMIT):
    """Recent requested items (orders) for AWS-SOP-Agent-* catalogs, with status + fields."""
    if cat_item_sys_id:
        items = snow.get("sc_cat_item", f"sys_id={cat_item_sys_id}", ["sys_id", "name"], 1)
    else:
        items = snow.get("sc_cat_item", f"nameSTARTSWITH{SOP_AGENT_PREFIX}", ["sys_id", "name"], 1000)
    id_to_name = {i["sys_id"]: i["name"] for i in items}
    if not id_to_name:
        return []

    ids = ",".join(id_to_name.keys())
    rows = snow.get(
        "sc_req_item",
        f"cat_itemIN{ids}^ORDERBYDESCsys_created_on",
        ["sys_id", "number", "state", "cat_item", "sys_created_on"],
        limit, display_value="all",
    )
    out = []
    for r in rows:
        ritm_id = snow.unwrap(r.get("sys_id"))
        out.append({
            "number": snow.unwrap(r.get("number")),
            "catalog": id_to_name.get(snow.unwrap(r.get("cat_item")), "-"),
            "state": snow.display(r.get("state")),
            "created": snow.unwrap(r.get("sys_created_on")),
            "fields": _read_ritm_vars(ritm_id),
        })
    return out


def _safe(fn, *args):
    """Run a cleanup call; swallow errors so tidy-up never blocks the real delete."""
    try:
        return fn(*args)
    except Exception:
        return None


def delete_catalog(sys_id):
    # Tidy up choices -> variables -> attachments first, but treat every step as
    # best-effort: some instances reject the question_choices query, and deleting
    # the catalog item below cascades to its variables/choices regardless.
    for v in (_safe(snow.get, "item_option_new", f"cat_item={sys_id}", ["sys_id"], 200) or []):
        for c in (_safe(snow.get, choices_table(), f"question={v['sys_id']}", ["sys_id"], 200) or []):
            _safe(snow.delete, choices_table(), c["sys_id"])
        _safe(snow.delete, "item_option_new", v["sys_id"])

    for a in (_safe(snow.get, "sys_attachment",
                    f"table_name=sc_cat_item^table_sys_id={sys_id}", ["sys_id"], 200) or []):
        _safe(snow.delete, "sys_attachment", a["sys_id"])

    # The essential step - this must succeed.
    snow.delete("sc_cat_item", sys_id)
    return True
