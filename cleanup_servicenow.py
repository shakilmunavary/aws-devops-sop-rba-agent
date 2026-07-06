#!/usr/bin/env python3"""cleanup_orders.pyDelete all orders (Requested Items + their now-empty parent Requests) that wereplaced against the AWS-SOP-Agent-* catalogs. Also delete the catalog items
themselves. Use this to reset a test environment.

IMPORTANT: deleting a catalog item does NOT delete the orders placed from it -
RITMs (sc_req_item) and REQs (sc_request) live on independently. This script
clears them all. This is destructive; it is a DRY RUN unless you pass --yes.

Usage:
  python cleanup_orders.py                  # DRY RUN: list what would be deleted
  python cleanup_orders.py --yes            # actually delete (all prefix catalogs)
  python cleanup_orders.py --yes --cat AWS-SOP-Agent-PowerManagement   # one catalog
"""

import os
import sys
import argparse

import snow_client as snow

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass

SOP_AGENT_PREFIX = os.getenv("SOP_AGENT_PREFIX", "AWS-SOP-Agent-")


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        print(f"   ! {fn.__name__}{args[:2]} -> {exc}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Delete orders for AWS-SOP-Agent-* catalogs")
    parser.add_argument("--yes", action="store_true", help="actually delete (otherwise dry run)")
    parser.add_argument("--cat", help="limit to one catalog item by exact name")
    args = parser.parse_args()

    miss = snow.missing_config()
    if miss:
        print("Missing config in .env:", ", ".join(miss))
        sys.exit(1)

    query = f"name={args.cat}" if args.cat else f"nameSTARTSWITH{SOP_AGENT_PREFIX}"
    items = snow.get("sc_cat_item", query, ["sys_id", "name"], 1000)
    if not items:
        print("No matching catalogs found.")
        return
    id_to_name = {i["sys_id"]: i["name"] for i in items}
    print("Catalogs:", ", ".join(id_to_name.values()))

    ids = ",".join(id_to_name.keys())
    ritms = snow.get("sc_req_item", f"cat_itemIN{ids}^ORDERBYsys_created_on",
                     ["sys_id", "number", "request", "cat_item"], 1000, display_value="all")
    if not ritms:
        print("No orders to delete.")
    else:
        print(f"\nFound {len(ritms)} order(s):")
        req_ids = set()
        for r in ritms:
            print(f"  - {snow.unwrap(r.get('number'))}  ({id_to_name.get(snow.unwrap(r.get('cat_item')), '?')})")
            rid = snow.unwrap(r.get("request"))
            if rid:
                req_ids.add(rid)

        if not args.yes:
            print(f"\nDRY RUN. Would delete {len(ritms)} RITM(s) and up to {len(req_ids)} REQ(s).")
            print("Re-run with --yes to delete.")
            return

        print("\nDeleting orders...")
        for r in ritms:
            ritm_id = snow.unwrap(r.get("sys_id"))
            num = snow.unwrap(r.get("number"))
            # variable links and catalog tasks first (best-effort), then the RITM
            for m in (_safe(snow.get, "sc_item_option_mtom", f"request_item={ritm_id}", ["sys_id"], 200) or []):
                _safe(snow.delete, "sc_item_option_mtom", m["sys_id"])
            for t in (_safe(snow.get, "sc_task", f"request_item={ritm_id}", ["sys_id"], 200) or []):
                _safe(snow.delete, "sc_task", t["sys_id"])
            _safe(snow.delete, "sc_req_item", ritm_id)
            print(f"  deleted {num}")

        # Delete parent requests that no longer have any RITMs.
        removed_reqs = 0
        for rid in req_ids:
            remaining = _safe(snow.get, "sc_req_item", f"request={rid}", ["sys_id"], 1)
            if not remaining:
                if _safe(snow.delete, "sc_request", rid):
                    removed_reqs += 1
        print(f"\nDone. Deleted {len(ritms)} RITM(s) and {removed_reqs} empty REQ(s).")

    # Finally, delete the catalog items themselves
    if args.yes:
        print("\nDeleting catalog items...")
        for cid, cname in id_to_name.items():
            if _safe(snow.delete, "sc_cat_item", cid):
                print(f"  deleted catalog item {cname}")
    else:
        print(f"\nDRY RUN. Would delete {len(id_to_name)} catalog item(s).")


if __name__ == "__main__":
    main()
