aws-sop-agent
A web console + polling engine that connects ServiceNow Service Catalog orders to
an AWS DevOps Agent. You create "AWS-SOP-Agent-*" catalog items from the UI; the
engine dispatches open (and approved) orders to the agent's webhook; the agent
performs the action and updates the ServiceNow ticket.
Files (all in project root)
```
app.py               Flask web console (catalogs + orders + engine control)
index.html           single-page UI (served from root, no templates/ folder)
snow_client.py       shared ServiceNow REST client
catalog_service.py   create / list / delete catalogs + list orders (auto-detects choices table)
engine_core.py       polling engine: dispatch new + approved orders, HMAC-sign, log
pythonengine.py      headless engine runner (optional)
cleanup_orders.py    delete all orders (RITMs + empty REQs) for the prefix catalogs
requirements.txt     deps: requests, python-dotenv, flask
.env.example         copy to .env and fill in
samples/             example SOPs to paste into the editor
```
Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in values
python app.py                 # open http://<host>:<APP_PORT><URL_PREFIX>
```
.env
```
SNOW_INSTANCE=...             # dev123456.service-now.com (trailing / and https:// tolerated)
SNOW_USER=admin
SNOW_PASSWORD=...
AWS_AGENT_WEBHOOK_URL=...     # the AWS DevOps Agent generic webhook
AWS_AGENT_HMAC_KEY=...        # shared HMAC secret
SOP_AGENT_PREFIX=AWS-SOP-Agent-
OPEN_STATE_QUERY=state=1      # what counts as a fresh order
APP_PORT=6777
URL_PREFIX=/awsagent          # "" to serve at /
# optional payload enrichment:
# AWS_ACCOUNT_ID=...
# AWS_REGION=us-west-2
# AWS_SERVICE=AWSPowerManagement
```
How it works
User orders an `AWS-SOP-Agent-*` catalog item (Instance IDs + Action).
Engine finds the open order, reads its variables + the SOP from the catalog
description, builds an incident-shaped payload, signs it
(HMAC-SHA256 over "{timestamp}:{body}", base64, header `x-amzn-event-signature`),
POSTs to the agent webhook, and sets the RITM to Work in Progress.
The agent reads the SOP. If the order needs approval (per the catalog's SOP,
e.g. PROD-tagged instances) it creates a ServiceNow approval routed to the
approver named in the SOP and stops. Otherwise it performs the action and
closes the ticket via its ServiceNow MCP.
When a human approves, the engine re-dispatches the order ONCE with
`data.approvalStatus = "approved"`, and the agent proceeds.
Agent-side requirements (outside this repo)
AWS MCP with start/stop/restart actions.
ServiceNow MCP with at least: `snow_update_request_item`, `snow_table_create`
(to create approvals), `snow_get_user` (to resolve the approver username).
Agent instructions that follow each order's SOP (sent in `data.sop.content`).
Reset a test environment
```bash
python cleanup_orders.py          # DRY RUN - lists orders it would delete
python cleanup_orders.py --yes    # delete RITMs + empty REQs (run before deleting a catalog)
