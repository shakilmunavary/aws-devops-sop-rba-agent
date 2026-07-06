requests>=2.31.0
python-dotenv>=1.0.0
flask>=3.0.0
root@ip-10-0-153-61:/home/rba/aws-sop-agent# cat snow_client.py
#!/usr/bin/env python3
"""
snow_client.py

Thin, shared ServiceNow REST helper used by both the web UI (app.py) and the
polling engine (engine_core.py). All connection details come from environment /
a .env file in the project root - nothing is hardcoded.

Env:
  SNOW_INSTANCE   dev123456.service-now.com   (a trailing "/" or "https://" is tolerated)
  SNOW_USER       admin
  SNOW_PASSWORD   <pdi password>
"""

import os
import json

import requests

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass


def normalize_instance(value):
    value = (value or "").strip()
    value = value.replace("https://", "").replace("http://", "")
    return value.rstrip("/")


def _cfg():
    return (
        normalize_instance(os.getenv("SNOW_INSTANCE", "")),
        os.getenv("SNOW_USER", ""),
        os.getenv("SNOW_PASSWORD", ""),
    )


def missing_config():
    instance, user, password = _cfg()
    missing = []
    if not instance:
        missing.append("SNOW_INSTANCE")
    if not user:
        missing.append("SNOW_USER")
    if not password:
        missing.append("SNOW_PASSWORD")
    return missing


def _base():
    instance, _, _ = _cfg()
    return f"https://{instance}/api/now"


def _auth():
    _, user, password = _cfg()
    return (user, password)


_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}


def _check(r):
    """Raise an informative error that includes ServiceNow's own message body."""
    if r.status_code < 400:
        return r
    detail = ""
    try:
        body = r.json()
        err = body.get("error") or {}
        detail = err.get("message") or err.get("detail") or json.dumps(body)
    except Exception:
        detail = (r.text or "")[:300]
    raise requests.HTTPError(f"HTTP {r.status_code} on {r.request.method} {r.url} -> {detail}")


# --------------------------------------------------------------------------- #
# Table API
# --------------------------------------------------------------------------- #
def get(table, query, fields=None, limit=100, display_value=None):
    params = {"sysparm_query": query, "sysparm_limit": limit}
    if fields:
        params["sysparm_fields"] = ",".join(fields)
    if display_value:
        params["sysparm_display_value"] = display_value
    r = requests.get(f"{_base()}/table/{table}", auth=_auth(), headers=_HEADERS, params=params, timeout=30)
    _check(r)
    return r.json().get("result", [])


def create(table, body):
    r = requests.post(f"{_base()}/table/{table}", auth=_auth(), headers=_HEADERS,
                      data=json.dumps(body), timeout=30)
    _check(r)
    return r.json()["result"]


def update(table, sys_id, body):
    r = requests.patch(f"{_base()}/table/{table}/{sys_id}", auth=_auth(), headers=_HEADERS,
                       data=json.dumps(body), timeout=30)
    _check(r)
    return r.json()["result"]


def delete(table, sys_id):
    r = requests.delete(f"{_base()}/table/{table}/{sys_id}", auth=_auth(), headers=_HEADERS, timeout=30)
    if r.status_code not in (200, 204):
        _check(r)
    return True


def attach(table, sys_id, file_name, content_bytes, content_type="text/markdown"):
    url = f"{_base()}/attachment/file"
    params = {"table_name": table, "table_sys_id": sys_id, "file_name": file_name}
    r = requests.post(url, auth=_auth(), params=params,
                      headers={"Content-Type": content_type, "Accept": "application/json"},
                      data=content_bytes, timeout=30)
    _check(r)
    return r.json().get("result", {})


def unwrap(v):
    """display_value=all returns {'value':..,'display_value':..}; normalise to the stored value."""
    if isinstance(v, dict):
        return v.get("value", v.get("display_value", ""))
    return v


def display(v):
    """Like unwrap(), but prefer the human-readable display_value."""
    if isinstance(v, dict):
        return v.get("display_value", v.get("value", ""))
    return v
