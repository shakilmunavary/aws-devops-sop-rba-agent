import requestsfrom requests.auth import HTTPBasicAuth# Replace with your instance detailsINSTANCE = "https://dev285883.service-now.com"USER = "admin"PASSWORD = "Magic@100"# Table creation endpointurl = f"{INSTANCE}/api/now/table/sys_db_object"# Define the new tabletable_payload = {
    "name": "u_aws_sop_tasks",       # internal name
    "label": "AWS SOP Tasks",        # display label
    "super_class": "task",           # optional: inherit from task table
    "access": "public"               # makes table globally accessible
}

# Create the table
response = requests.post(url, auth=HTTPBasicAuth(USER, PASSWORD), json=table_payload)
print("Table creation status:", response.status_code, response.text)

# Now add columns (dictionary entries)
columns = [
    {"element": "u_type", "column_label": "Type", "internal_type": "string"},
    {"element": "payload", "column_label": "Payload", "internal_type": "string"},
    {"element": "u_sop_definition", "column_label": "SOP Definition", "internal_type": "string"},
    {"element": "description", "column_label": "Description", "internal_type": "string"},
    {"element": "name", "column_label": "Name", "internal_type": "string"},
    {"element": "state", "column_label": "State", "internal_type": "string"},
    {"element": "approval", "column_label": "Approval", "internal_type": "string"},
    {"element": "number", "column_label": "Number", "internal_type": "string"}
]

dict_url = f"{INSTANCE}/api/now/table/sys_dictionary"

for col in columns:
    col["name"] = "u_aws_sop_tasks"   # attach to our table
    resp = requests.post(dict_url, auth=HTTPBasicAuth(USER, PASSWORD), json=col)
    print(f"Column {col['element']} creation:", resp.status_code, resp.text)
