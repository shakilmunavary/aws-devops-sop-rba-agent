## AWS SOP Engine Approval Framework

**Version:** 1.0
**Author:** Cognizant AWS SOP Engine Team
**Purpose:** Approval Framework Implementation Guide

---

# 1. Background

## Why This Was Needed

Originally the AWS SOP Engine attempted to create approval records using the ServiceNow Table API.

### Original Design

```text
Python Engine
       ↓
ServiceNow Table API
       ↓
sysapproval_approver
```

Although the record was created, ServiceNow stripped critical reference fields.

Missing fields:

```text
Approver
SysApproval
```

This caused:

- My Approvals to remain empty
- Approval Related Lists to remain empty
- Approval workflow to fail

---

# 2. Final Solution

We implemented a ServiceNow Scripted REST API.

```text
Python SOP Engine
        ↓
Scripted REST API
        ↓
GlideRecord
        ↓
sysapproval_approver
```

This preserved:

```text
Approver
Approval Record
RITM Relationship
```

Result:

✅ My Approvals populated

✅ Approval Related List populated

✅ Approval Workflow functional

---

# 3. Login To ServiceNow

Login to ServiceNow:

```text
https://<instance>.service-now.com
```

Use an Administrator account.

Example:

```text
admin
```

---

# 4. Create Scripted REST API

Navigate to:

```text
System Web Services
    → Scripted REST APIs
```

Click:

```text
New
```

Fill the fields below.

## Name

```text
AWS SOP API
```

## API ID

```text
aws_sop_api
```

## Application

```text
Global
```

## Protection Policy

```text
None
```

Save.

---

# 5. Configure Default ACL

Select:

```text
Scripted REST External Default
```

This allows authenticated external access.

Save.

After saving, ServiceNow generates a base path:

Example:

```text
/api/1776488/aws_sop_api
```

Note:

The numeric namespace will differ by instance.

---

# 6. Create API Resource

Open:

```text
AWS SOP API
```

Scroll to:

```text
Resources
```

Click:

```text
New
```

---

# 7. Configure Resource

## Resource Name

```text
createApproval
```

## HTTP Method

```text
POST
```

## Relative Path

```text
/createApproval
```

Resulting endpoint:

```text
POST
/api/1776488/aws_sop_api/createApproval
```

---

# 8. Resource Security Configuration

Set:

```text
Requires Authentication = TRUE
```

Set:

```text
Requires ACL Authorization = FALSE
```

Reason:

The Python SOP Engine already authenticates using:

```text
SNOW_USER
SNOW_PASSWORD
```

from the `.env` configuration.

---

# 9. Resource Script

Paste the following code.

```javascript
(function process(request, response) {

    var body = request.body.data;

    var gr =
        new GlideRecord(
            'sysapproval_approver'
        );

    gr.initialize();

    gr.approver =
        body.approver;

    gr.sysapproval =
        body.sysapproval;

    gr.state =
        "requested";

    gr.comments =
        "Created from AWS SOP Agent";

    var id =
        gr.insert();

    var verify =
        new GlideRecord(
            'sysapproval_approver'
        );

    verify.get(id);

    response.setBody({
        success : true,
        sys_id : id,
        approver :
            verify.approver.toString(),
        sysapproval :
            verify.sysapproval.toString()
    });

})(request,response);
```

Save.

Publish.

---

# 10. Verify API Creation

Verify:

```text
API Name
```

```text
AWS SOP API
```

Verify:

```text
Resource
```

```text
createApproval
```

Verify:

```text
Path
```

```text
/api/1776488/aws_sop_api/createApproval
```

---

# 11. Configure Python Engine

Add to `.env`:

```ini
SNOW_APPROVAL_API_URL=https://<instance>.service-now.com/api/1776488/aws_sop_api/createApproval
```

Example:

```ini
SNOW_APPROVAL_API_URL=https://dev285883.service-now.com/api/1776488/aws_sop_api/createApproval
```

No hard-coded URLs should exist in `main.py`.

---

# 12. Python Validation Script

Create:

```python
import requests
import json

url = (
    "https://INSTANCE.service-now.com"
    "/api/1776488/aws_sop_api/createApproval"
)

payload = {
    "approver":
        "268a7306c3398b1472d5f61d050131a9",

    "sysapproval":
        "122d38b6c3b1831072d5f61d05013175"
}

response = requests.post(
    url,
    auth=("admin", "password"),
    json=payload
)

print(
    json.dumps(
        response.json(),
        indent=2
    )
)
```

Run:

```bash
python3 test_gliderecord_api.py
```

---

# 13. Expected Response

Example:

```json
{
  "result": {
    "success": true,
    "sys_id": "e68a5576c3b5831072d5f61d050131b2",
    "approver": "268a7306c3398b1472d5f61d050131a9",
    "sysapproval": "898a1576c3b5831072d5f61d05013153"
  }
}
```

---

# 14. Verify Approval Record

Run:

```bash
curl -u admin:PASSWORD \
"https://INSTANCE.service-now.com/api/now/table/sysapproval_approver?sysparm_display_value=all"
```

Expected:

```text
Approver = SOP APPROVER
SysApproval = RITM0010023
State = Requested
```

---

# 15. Submit Catalog Request

Submit:

```text
AWS-SOP-Agent-PowerManagement
```

Fill:

```text
Instance ID
Action
```

Example:

```text
Instance ID = i-xxxxxxxx
Action = Start
```

Submit request.

---

# 16. Engine Creates Approval

Expected logs:

```text
APPROVAL REQUEST
```

Then:

```text
SCRIPTED REST RAW RESPONSE
```

Then:

```text
approval requested from SOP APPROVER
```

---

# 17. Approver Action

Login as:

```text
SOP APPROVER
```

Navigate:

```text
My Approvals
```

Approval should appear.

Approve request.

---

# 18. Approval State Tracking

## Initial Discovery

RITM field:

```text
sc_req_item.approval
```

remained:

```text
requested
```

even after approval.

---

## Final Design

The SOP Engine tracks:

```python
sysapproval_approver.state
```

instead of:

```python
sc_req_item.approval
```

This accurately reflects approval status.

---

# 19. Dispatch To AWS DevOps Agent

When:

```text
sysapproval_approver.state
```

becomes:

```text
approved
```

The engine executes:

```python
dispatch_order()
```

Expected log:

```text
approval granted -> dispatching
```

Then:

```text
SOP-ENGINE-DISPATCHED-AFTER-APPROVAL
```

---

# 20. Lessons Learned

## What Failed

```text
Table API
```

Reason:

```text
Approver stripped
SysApproval stripped
```

---

## What Worked

```text
Scripted REST API
        ↓
GlideRecord
        ↓
sysapproval_approver
```

---

## Approval State Challenge

Do NOT depend on:

```python
sc_req_item.approval
```

Use:

```python
sysapproval_approver.state
```

---

# 21. Final Architecture

```text
ServiceNow Catalog
        ↓
AWS SOP Engine
        ↓
Scripted REST API
        ↓
GlideRecord
        ↓
sysapproval_approver
        ↓
Approver
        ↓
Approved
        ↓
AWS DevOps Agent
        ↓
Update RITM
```

---

# 22. Summary

The ServiceNow Scripted REST API became a mandatory solution component because direct Table API insertion failed to maintain approval relationships.

The final implementation:

✅ Creates Approval Records

✅ Preserves Approver Relationships

✅ Integrates With My Approvals

✅ Supports ServiceNow Governance

✅ Enables Approval Driven Automation

✅ Successfully Dispatches Approved Requests To AWS DevOps Agent

---
