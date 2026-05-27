# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Example demonstrating:  
1. Access variable group values from Python. Note for sensitive variables ensure the variable group is linked to key vault. See https://learn.microsoft.com/en-us/azure/devops/pipelines/library/link-variable-groups-to-key-vaults?view=azure-devops
2. Use of Service Principal Name (SPN) with a Secret credential flow, leveraging the ClientSecretCredential class. 
3. Use the Fabric reset APIs to lookup the workspace ID based on workspace name
4. Using debug log level
5. Discover warehouse connection info and SQL endpoints for DACPAC deployment
"""
# START-EXAMPLE

# argparse is required to gracefully deal with the arguments
import os,argparse, requests, ast, json
from fabric_cicd import FabricWorkspace, publish_all_items, unpublish_all_orphan_items,change_log_level
from azure.identity import ClientSecretCredential

# function to return the workspace ID
def get_workspace_id(p_ws_name, p_token):
    url = "https://api.fabric.microsoft.com/v1/workspaces"
    headers = {
        "Authorization": f"Bearer {p_token.token}",
        "Content-Type": "application/json"
    }

    response = requests.get(url, headers=headers)
    ws_id =''
    if response.status_code == 200:
        workspaces = response.json()["value"]
        for workspace in workspaces:
            if workspace["displayName"] == p_ws_name:
                ws_id = workspace["id"] 
                return workspace["id"]
        if ws_id == '':
            return f"Error: Workspace {p_ws_name} could not found."
    else:
        return f"Error: {response.status_code}, {response.text}"

def find_warehouse_name(repo_root, repository_directory):
    """Discover warehouse name from .platform metadata in the repo."""
    env_name = os.environ.get("WAREHOUSE_NAME")
    if env_name:
        return env_name

    search_root = os.path.join(repo_root, repository_directory)
    for root, _, files in os.walk(search_root):
        if ".platform" not in files:
            continue
        platform_path = os.path.join(root, ".platform")
        try:
            with open(platform_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            metadata = payload.get("metadata", {})
            if metadata.get("type") == "Warehouse":
                display_name = metadata.get("displayName")
                if display_name:
                    return display_name
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return None

def list_warehouses(workspace_id, token):
    url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/warehouses"
    headers = {
        "Authorization": f"Bearer {token.token}",
        "Content-Type": "application/json"
    }
    warehouses = []
    continuation_token = None
    while True:
        params = {}
        if continuation_token:
            params["continuationToken"] = continuation_token
        response = requests.get(url, headers=headers, params=params)
        if response.status_code != 200:
            raise ValueError(f"Error: {response.status_code}, {response.text}")
        payload = response.json()
        warehouses.extend(payload.get("value", []))
        continuation_token = payload.get("continuationToken")
        if not continuation_token:
            break
    return warehouses

def get_warehouse_connection(workspace_id, token, warehouse_name):
    warehouses = list_warehouses(workspace_id, token)
    for warehouse in warehouses:
        if warehouse.get("displayName") == warehouse_name:
            properties = warehouse.get("properties", {})
            return properties.get("connectionString"), warehouse.get("id")
    return None, None

def list_sql_endpoints(workspace_id, token):
    url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/sqlEndpoints"
    headers = {
        "Authorization": f"Bearer {token.token}",
        "Content-Type": "application/json"
    }
    endpoints = []
    continuation_token = None
    while True:
        params = {}
        if continuation_token:
            params["continuationToken"] = continuation_token
        response = requests.get(url, headers=headers, params=params)
        if response.status_code != 200:
            raise ValueError(f"Error: {response.status_code}, {response.text}")
        payload = response.json()
        endpoints.extend(payload.get("value", []))
        continuation_token = payload.get("continuationToken")
        if not continuation_token:
            break
    return endpoints

def get_sql_endpoint_connection(workspace_id, token, sql_endpoint_id):
    url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/sqlEndpoints/{sql_endpoint_id}/connectionString"
    headers = {
        "Authorization": f"Bearer {token.token}",
        "Content-Type": "application/json"
    }
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise ValueError(f"Error: {response.status_code}, {response.text}")
    return response.json().get("connectionString")

def build_sql_endpoints_payload(workspace_id, token, label):
    sql_endpoints = list_sql_endpoints(workspace_id, token)
    if not sql_endpoints:
        print(f"No SQL endpoints found for {label} workspace.")
        return []
    endpoints_with_conn = []
    for ep in sql_endpoints:
        ep_id = ep.get("id")
        ep_name = ep.get("displayName")
        try:
            conn_str = get_sql_endpoint_connection(workspace_id, token, ep_id)
            endpoints_with_conn.append({
                "displayName": ep_name,
                "id": ep_id,
                "connectionString": conn_str
            })
        except Exception as e:
            print(f"Warning: Could not get connection string for {ep_name}: {str(e)}")
    if endpoints_with_conn:
        print(f"Found {len(endpoints_with_conn)} SQL endpoint(s) in {label} workspace: {', '.join([ep.get('displayName') for ep in endpoints_with_conn])}")
    return endpoints_with_conn

# set log level
change_log_level("DEBUG")

# parse arguments from yaml pipeline. These are typically secrets from a variable group linked to an Azure Key Vault
parser = argparse.ArgumentParser(description='Process Azure Pipeline arguments.')
parser.add_argument('--aztenantid',type=str, help= 'tenant ID')
parser.add_argument('--azclientid',type=str, help= 'SP client ID')
parser.add_argument('--azspsecret',type=str, help= 'SP secret')
parser.add_argument('--workspace_name',type=str, help= 'Target Fabric workspace name')
parser.add_argument('--items_in_scope',type=str, help= 'Defines the item types to be deployed')
args = parser.parse_args()
item_types_in_scope = args.items_in_scope

#get the token#
print('Obtaining token...')
token_credential = ClientSecretCredential(client_id=args.azclientid, client_secret=args.azspsecret, tenant_id=args.aztenantid)

# workspace name passed explicitly from pipeline stage
workspace_name = args.workspace_name
print(f'Target workspace: {workspace_name}')

# generating the token used to call the Fabric REST API
resource = 'https://api.fabric.microsoft.com/'
scope = f'{resource}.default'
print(f'scope set to {scope}')
token = token_credential.get_token(scope)

# call the workspace ID lookup function
lookup_response = get_workspace_id(workspace_name, token)
if lookup_response.startswith("Error"):
    errmsg=f"{lookup_response}. Perhaps workspace name is set incorrectly in the variable group of does not map to branch name + 'WorkspaceName'"
    raise ValueError(errmsg)
else:
    wks_id = lookup_response
    print(f"Workspace ID for {workspace_name} set to {wks_id}")

# set repo folder based on the variable group value of gitDirectory
repository_directory = os.environ["GITDIRECTORY"]

repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# convert the item types argument into a valid list
item_types = [item.strip().strip('"').strip("'") for item in args.items_in_scope.strip("[]").split(",")]

# Initialize the FabricWorkspace object with the required parameters
target_workspace = FabricWorkspace(
    workspace_id=wks_id,
    environment=workspace_name,
    repository_directory=repository_directory,
    item_type_in_scope=item_types,
    token_credential=token_credential,
)

# Publish items to the workspace
print(f'Publish branch to workspace...')
publish_all_items(target_workspace)

# Unpublish orphaned items from the workspace
unpublish_all_orphan_items(target_workspace)

# ============================================================================
# Discover Warehouse connection info for DACPAC deployment
# ============================================================================
warehouse_name = find_warehouse_name(repo_root, repository_directory)
if warehouse_name:
    print(f"Resolving connection string for warehouse {warehouse_name}...")
    warehouse_conn, warehouse_id = get_warehouse_connection(wks_id, token, warehouse_name)
    if warehouse_conn:
        print(f"##vso[task.setvariable variable=FABRIC_WAREHOUSE_SERVER]{warehouse_conn}")
        print(f"##vso[task.setvariable variable=FABRIC_WAREHOUSE_NAME]{warehouse_name}")
        if warehouse_id:
            print(f"##vso[task.setvariable variable=FABRIC_WAREHOUSE_ID]{warehouse_id}")
    else:
        print(f"Warning: Warehouse {warehouse_name} was not found in workspace {workspace_name}.")
else:
    print("No warehouse found in repository metadata — DACPAC publish will be skipped.")

# Discover SQL endpoints for lakehouse schema deployment
print("Discovering SQL endpoints for lakehouses in target workspace...")
target_endpoints = build_sql_endpoints_payload(wks_id, token, "target")
if target_endpoints:
    print(f"##vso[task.setvariable variable=FABRIC_SQL_ENDPOINTS_TARGET]{json.dumps(target_endpoints)}")
