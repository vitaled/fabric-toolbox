# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Post-deployment operations for Fabric CI/CD Option 2.

After fabric-cicd publishes items to the target workspace, this script
performs environment-specific rebinding:

1. Data Pipeline rebinding — connections, linked services, notebook/pipeline/
   semantic model activity references
2. Notebook rebinding — default Lakehouse and Warehouse dependencies
3. Semantic Model rebinding — Direct Lake connections and M expression updates
4. Report rebinding — rebinds reports to their semantic models in the target workspace

Runs in an Azure DevOps pipeline agent using Service Principal authentication.
"""

import argparse
import base64
import json
import re
import sys
import time

import requests
from azure.identity import ClientSecretCredential


# ============================================================================
# Fabric REST API Client
# ============================================================================

class FabricClient:
    """Thin wrapper around the Fabric REST API."""

    BASE_URL = "https://api.fabric.microsoft.com/v1"

    def __init__(self, token_credential):
        self._credential = token_credential
        self._token = None

    @property
    def _headers(self):
        if self._token is None or self._token.expires_on < time.time() + 60:
            self._token = self._credential.get_token("https://api.fabric.microsoft.com/.default")
        return {
            "Authorization": f"Bearer {self._token.token}",
            "Content-Type": "application/json",
        }

    # --- generic helpers ---

    def get(self, path, **kwargs):
        return requests.get(f"{self.BASE_URL}/{path}", headers=self._headers, **kwargs)

    def post(self, path, **kwargs):
        return requests.post(f"{self.BASE_URL}/{path}", headers=self._headers, **kwargs)

    def patch(self, path, **kwargs):
        return requests.patch(f"{self.BASE_URL}/{path}", headers=self._headers, **kwargs)

    def wait_for_long_running_operation(self, response, timeout=600):
        """Poll a 202 long-running operation until completion."""
        if response.status_code not in (200, 201, 202):
            return response
        location = response.headers.get("Location")
        if not location:
            return response
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            poll = requests.get(location, headers=self._headers)
            if poll.status_code != 200:
                return poll
            status = poll.json().get("status", "")
            if status in ("Completed", "Succeeded"):
                return poll
            if status in ("Failed", "Cancelled"):
                print(f"  [ERROR] Long-running operation {status}: {poll.text}")
                return poll
        print("  [WARN] Operation timed out")
        return response

    # --- workspace items ---

    def list_items(self, workspace_id, item_type=None):
        """List all items in a workspace, optionally filtered by type."""
        items = []
        url = f"workspaces/{workspace_id}/items"
        if item_type:
            url += f"?type={item_type}"
        while url:
            resp = self.get(url)
            resp.raise_for_status()
            data = resp.json()
            items.extend(data.get("value", []))
            url = data.get("continuationUri")
        return items

    def resolve_item_id(self, workspace_id, item_name, item_type):
        """Find an item's ID by name and type in the target workspace."""
        items = self.list_items(workspace_id, item_type)
        for item in items:
            if item["displayName"] == item_name:
                return item["id"]
        return None

    def resolve_item_name(self, workspace_id, item_id, item_type=None):
        """Find an item's name by ID."""
        items = self.list_items(workspace_id, item_type)
        for item in items:
            if item["id"] == item_id:
                return item["displayName"]
        return None

    # --- item definitions ---

    def get_item_definition(self, workspace_id, item_id, format=None):
        """Get the definition of a Fabric item (returns parts list)."""
        url = f"workspaces/{workspace_id}/items/{item_id}/getDefinition"
        if format:
            url += f"?format={format}"
        resp = self.post(url)
        if resp.status_code == 202:
            resp = self.wait_for_long_running_operation(resp)
            # After LRO completes, the body may contain the definition
            if resp.status_code == 200:
                body = resp.json()
                if "definition" in body:
                    return body["definition"]["parts"]
                # Some LROs return a redirect
                result_url = body.get("resourceLocation")
                if result_url:
                    resp2 = requests.get(result_url, headers=self._headers)
                    if resp2.status_code != 200:
                        print(f"    [ERROR] getDefinition redirect failed: {resp2.status_code} {resp2.text[:200]}")
                        return []
                    return resp2.json().get("definition", {}).get("parts", [])
            else:
                print(f"    [ERROR] getDefinition LRO failed: {resp.status_code} {resp.text[:200]}")
                return []
        elif resp.status_code == 200:
            return resp.json().get("definition", {}).get("parts", [])
        else:
            print(f"    [ERROR] getDefinition failed: {resp.status_code} {resp.text[:200]}")
            return []
        return []

    def get_semantic_model_definition(self, workspace_id, model_id):
        """Get a Semantic Model definition using the type-specific API with TMDL format."""
        resp = self.post(
            f"workspaces/{workspace_id}/semanticModels/{model_id}/getDefinition?format=TMDL"
        )
        if resp.status_code == 202:
            # LRO — poll the Location header, then fetch result
            location = resp.headers.get("Location")
            if not location:
                print(f"    [ERROR] SM getDefinition 202 but no Location header")
                return []
            deadline = time.time() + 600
            while time.time() < deadline:
                time.sleep(5)
                poll = requests.get(location, headers=self._headers)
                if poll.status_code != 200:
                    print(f"    [ERROR] SM getDefinition LRO poll failed: {poll.status_code}")
                    return []
                status = poll.json().get("status", "")
                if status in ("Completed", "Succeeded"):
                    # Fetch the actual result from /result endpoint
                    result_url = location.rstrip("/") + "/result"
                    resp2 = requests.get(result_url, headers=self._headers)
                    if resp2.status_code != 200:
                        print(f"    [ERROR] SM getDefinition result fetch failed: {resp2.status_code} {resp2.text[:200]}")
                        return []
                    return resp2.json().get("definition", {}).get("parts", [])
                if status in ("Failed", "Cancelled"):
                    print(f"    [ERROR] SM getDefinition LRO {status}: {poll.text[:200]}")
                    return []
            print(f"    [WARN] SM getDefinition LRO timed out")
            return []
        elif resp.status_code == 200:
            return resp.json().get("definition", {}).get("parts", [])
        else:
            print(f"    [ERROR] SM getDefinition failed: {resp.status_code} {resp.text[:200]}")
            return []

    def update_semantic_model_definition(self, workspace_id, model_id, parts):
        """Update a Semantic Model definition using the type-specific API."""
        body = {"definition": {"parts": parts}}
        resp = self.post(
            f"workspaces/{workspace_id}/semanticModels/{model_id}/updateDefinition?updateMetadata=true",
            json=body,
        )
        if resp.status_code == 202:
            resp = self.wait_for_long_running_operation(resp)
        return resp

    def update_item_definition(self, workspace_id, item_id, parts):
        """Update a Fabric item definition."""
        body = {"definition": {"parts": parts}}
        resp = self.post(
            f"workspaces/{workspace_id}/items/{item_id}/updateDefinition",
            json=body,
        )
        if resp.status_code == 202:
            resp = self.wait_for_long_running_operation(resp)
        return resp

    # --- warehouse-specific ---

    def get_warehouse_connection_string(self, workspace_id, warehouse_id):
        """Get the SQL connection string for a warehouse."""
        resp = self.get(f"workspaces/{workspace_id}/warehouses/{warehouse_id}")
        resp.raise_for_status()
        return resp.json().get("properties", {}).get("connectionString", "")

    # --- connections ---

    def list_connections(self):
        """List all connections in the tenant."""
        items = []
        url = "connections"
        while url:
            resp = self.get(url)
            resp.raise_for_status()
            data = resp.json()
            items.extend(data.get("value", []))
            url = data.get("continuationUri")
        return items


# ============================================================================
# 1. Data Pipeline Rebinding
# ============================================================================

def rebind_data_pipelines(client, source_ws_id, target_ws_id, connection_mapping, target_stage):
    """
    Update Data Pipeline definitions in the target workspace:
    - Remap connection IDs
    - Remap Warehouse/Lakehouse linked services
    - Remap Notebook activity references
    - Remap InvokePipeline activity references
    - Remap SemanticModel refresh activity references
    """
    print("\n=== Post-Deployment: Data Pipeline Rebinding ===")

    pipelines = client.list_items(target_ws_id, "DataPipeline")
    if not pipelines:
        print("  No Data Pipelines found — skipping")
        return

    # Load valid tenant connections for validation
    tenant_connections = set()
    try:
        for conn in client.list_connections():
            tenant_connections.add(conn.get("id", ""))
    except Exception:
        pass  # Continue without validation

    for pipeline in pipelines:
        name = pipeline["displayName"]
        pid = pipeline["id"]
        print(f"  Processing pipeline: {name}")

        parts = client.get_item_definition(target_ws_id, pid)
        if not parts:
            print(f"    [WARN] Could not get definition — skipping")
            continue

        updated = False
        for part in parts:
            if part.get("path") != "pipeline-content.json":
                continue

            payload_b64 = part.get("payload", "")
            try:
                content = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
            except Exception as e:
                print(f"    [WARN] Could not decode pipeline content: {e}")
                continue

            # Apply all transformations
            c1 = _update_connections(content, connection_mapping, target_stage, tenant_connections)
            c2 = _update_linked_services(content, client, source_ws_id, target_ws_id)
            c3 = _update_notebook_refs(content, client, source_ws_id, target_ws_id)
            c4 = _update_pipeline_refs(content, client, source_ws_id, target_ws_id)
            c5 = _update_semantic_model_refs(content, client, source_ws_id, target_ws_id)

            if c1 or c2 or c3 or c4 or c5:
                new_payload = base64.b64encode(
                    json.dumps(content).encode("utf-8")
                ).decode("utf-8")
                part["payload"] = new_payload
                part["payloadType"] = "InlineBase64"
                updated = True

        if updated:
            resp = client.update_item_definition(target_ws_id, pid, parts)
            if resp.status_code in (200, 202):
                print(f"    Updated successfully")
            else:
                print(f"    [ERROR] Update failed: {resp.status_code} {resp.text}")
        else:
            print(f"    No changes needed")


def _find_connection_id(dev_connection_id, connection_mapping, target_stage, tenant_connections):
    """Map a dev connection ID to the target stage connection ID."""
    for mapping in connection_mapping:
        if mapping.get("ConnectionStage1") == dev_connection_id:
            target_col = f"ConnectionStage{target_stage}"
            target_id = mapping.get(target_col, "")
            if not target_id:
                return dev_connection_id
            if tenant_connections and target_id not in tenant_connections:
                print(f"    [WARN] Target connection {target_id} not found in tenant, using source")
                return dev_connection_id
            return target_id
    return dev_connection_id


def _update_connections(obj, connection_mapping, target_stage, tenant_connections):
    """Recursively replace connection IDs in the pipeline JSON."""
    changed = False
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "connection" and isinstance(value, str):
                new_val = _find_connection_id(value, connection_mapping, target_stage, tenant_connections)
                if new_val != value:
                    obj[key] = new_val
                    changed = True
            else:
                if _update_connections(value, connection_mapping, target_stage, tenant_connections):
                    changed = True
    elif isinstance(obj, list):
        for item in obj:
            if _update_connections(item, connection_mapping, target_stage, tenant_connections):
                changed = True
    return changed


def _update_linked_services(obj, client, source_ws_id, target_ws_id):
    """Recursively update Warehouse and Lakehouse linked service references."""
    changed = False
    if isinstance(obj, dict):
        if "linkedService" in obj and isinstance(obj["linkedService"], dict):
            props = obj["linkedService"].get("properties", {})
            type_props = props.get("typeProperties", {})
            svc_type = props.get("type", "")

            if svc_type == "DataWarehouse":
                src_artifact_id = type_props.get("artifactId", "")
                if src_artifact_id:
                    src_name = client.resolve_item_name(source_ws_id, src_artifact_id, "Warehouse")
                    if src_name:
                        tgt_id = client.resolve_item_id(target_ws_id, src_name, "Warehouse")
                        if tgt_id:
                            tgt_endpoint = client.get_warehouse_connection_string(target_ws_id, tgt_id)
                            type_props["artifactId"] = tgt_id
                            type_props["workspaceId"] = target_ws_id
                            if tgt_endpoint:
                                type_props["endpoint"] = tgt_endpoint
                            changed = True
                            print(f"    Linked service: Warehouse '{src_name}' -> {tgt_id}")

            elif svc_type == "Lakehouse":
                src_artifact_id = type_props.get("artifactId", "")
                if src_artifact_id:
                    src_name = client.resolve_item_name(source_ws_id, src_artifact_id, "Lakehouse")
                    if src_name:
                        tgt_id = client.resolve_item_id(target_ws_id, src_name, "Lakehouse")
                        if tgt_id:
                            type_props["artifactId"] = tgt_id
                            type_props["workspaceId"] = target_ws_id
                            changed = True
                            print(f"    Linked service: Lakehouse '{src_name}' -> {tgt_id}")

        for key, value in obj.items():
            if key != "linkedService":
                if _update_linked_services(value, client, source_ws_id, target_ws_id):
                    changed = True
    elif isinstance(obj, list):
        for item in obj:
            if _update_linked_services(item, client, source_ws_id, target_ws_id):
                changed = True
    return changed


def _update_notebook_refs(obj, client, source_ws_id, target_ws_id):
    """Recursively update TridentNotebook activity references."""
    changed = False
    if isinstance(obj, dict):
        if obj.get("type") == "TridentNotebook":
            type_props = obj.get("typeProperties", {})
            src_nb_id = type_props.get("notebookId", "")
            if src_nb_id:
                src_name = client.resolve_item_name(source_ws_id, src_nb_id, "Notebook")
                if src_name:
                    tgt_id = client.resolve_item_id(target_ws_id, src_name, "Notebook")
                    if tgt_id:
                        type_props["notebookId"] = tgt_id
                        type_props["workspaceId"] = target_ws_id
                        changed = True
                        print(f"    Activity ref: Notebook '{src_name}' -> {tgt_id}")
        for key, value in obj.items():
            if _update_notebook_refs(value, client, source_ws_id, target_ws_id):
                changed = True
    elif isinstance(obj, list):
        for item in obj:
            if _update_notebook_refs(item, client, source_ws_id, target_ws_id):
                changed = True
    return changed


def _update_pipeline_refs(obj, client, source_ws_id, target_ws_id):
    """Recursively update InvokePipeline (Fabric-to-Fabric) activity references."""
    changed = False
    if isinstance(obj, dict):
        if obj.get("type") == "InvokePipeline":
            type_props = obj.get("typeProperties", {})
            if type_props.get("operationType") == "InvokeFabricPipeline":
                src_pid = type_props.get("pipelineId", "")
                if src_pid:
                    src_name = client.resolve_item_name(source_ws_id, src_pid, "DataPipeline")
                    if src_name:
                        tgt_id = client.resolve_item_id(target_ws_id, src_name, "DataPipeline")
                        if tgt_id:
                            type_props["pipelineId"] = tgt_id
                            type_props["workspaceId"] = target_ws_id
                            changed = True
                            print(f"    Activity ref: Pipeline '{src_name}' -> {tgt_id}")
        for key, value in obj.items():
            if _update_pipeline_refs(value, client, source_ws_id, target_ws_id):
                changed = True
    elif isinstance(obj, list):
        for item in obj:
            if _update_pipeline_refs(item, client, source_ws_id, target_ws_id):
                changed = True
    return changed


def _update_semantic_model_refs(obj, client, source_ws_id, target_ws_id):
    """Recursively update PBISemanticModelRefresh activity references."""
    changed = False
    if isinstance(obj, dict):
        if obj.get("type") == "PBISemanticModelRefresh":
            type_props = obj.get("typeProperties", {})
            src_ds_id = type_props.get("datasetId", "")
            if src_ds_id:
                src_name = client.resolve_item_name(source_ws_id, src_ds_id, "SemanticModel")
                if src_name:
                    tgt_id = client.resolve_item_id(target_ws_id, src_name, "SemanticModel")
                    if tgt_id:
                        type_props["datasetId"] = tgt_id
                        type_props["groupId"] = target_ws_id
                        changed = True
                        print(f"    Activity ref: SemanticModel '{src_name}' -> {tgt_id}")
        for key, value in obj.items():
            if _update_semantic_model_refs(value, client, source_ws_id, target_ws_id):
                changed = True
    elif isinstance(obj, list):
        for item in obj:
            if _update_semantic_model_refs(item, client, source_ws_id, target_ws_id):
                changed = True
    return changed


# ============================================================================
# 2. Notebook Rebinding
# ============================================================================

def rebind_notebooks(client, source_ws_id, target_ws_id):
    """
    Update Notebook definitions in the target workspace to point their
    default Lakehouse and Warehouse to same-named items in the target workspace.
    """
    print("\n=== Post-Deployment: Notebook Rebinding ===")

    notebooks = client.list_items(target_ws_id, "Notebook")
    if not notebooks:
        print("  No Notebooks found — skipping")
        return

    for nb in notebooks:
        name = nb["displayName"]
        nb_id = nb["id"]
        print(f"  Processing notebook: {name}")

        parts = client.get_item_definition(target_ws_id, nb_id)
        if not parts:
            print(f"    [WARN] Could not get definition — skipping")
            continue

        updated = False
        for part in parts:
            if part.get("path") not in ("notebook-content.py", "notebook-content.ipynb"):
                continue

            payload_b64 = part.get("payload", "")
            try:
                content_str = base64.b64decode(payload_b64).decode("utf-8")
            except Exception as e:
                print(f"    [WARN] Could not decode: {e}")
                continue

            # For .py files, the metadata is in META comments; for .ipynb it's JSON
            if part["path"] == "notebook-content.ipynb":
                updated_content, did_change = _rebind_notebook_ipynb(
                    content_str, client, source_ws_id, target_ws_id, name
                )
            else:
                updated_content, did_change = _rebind_notebook_py(
                    content_str, client, source_ws_id, target_ws_id, name
                )

            if did_change:
                part["payload"] = base64.b64encode(
                    updated_content.encode("utf-8")
                ).decode("utf-8")
                part["payloadType"] = "InlineBase64"
                updated = True

        if updated:
            resp = client.update_item_definition(target_ws_id, nb_id, parts)
            if resp.status_code in (200, 202):
                print(f"    Updated successfully")
            else:
                print(f"    [ERROR] Update failed: {resp.status_code} {resp.text}")
        else:
            print(f"    No changes needed")


def _rebind_notebook_ipynb(content_str, client, source_ws_id, target_ws_id, nb_name):
    """Rebind lakehouse/warehouse refs in .ipynb JSON format."""
    try:
        nb_json = json.loads(content_str)
    except json.JSONDecodeError:
        return content_str, False

    metadata = nb_json.get("metadata", {})
    dependencies = metadata.get("dependencies", {})
    changed = False

    # Lakehouse rebinding
    lh_info = dependencies.get("lakehouse", {})
    lh_name = lh_info.get("default_lakehouse_name")
    if lh_name:
        tgt_lh_id = client.resolve_item_id(target_ws_id, lh_name, "Lakehouse")
        if tgt_lh_id:
            old_id = lh_info.get("default_lakehouse")
            if old_id != tgt_lh_id:
                lh_info["default_lakehouse"] = tgt_lh_id
                lh_info["default_lakehouse_workspace_id"] = target_ws_id
                # Update known_lakehouses list if present
                for kl in lh_info.get("known_lakehouses", []):
                    if kl.get("id") == old_id:
                        kl["id"] = tgt_lh_id
                changed = True
                print(f"    Lakehouse '{lh_name}' -> {tgt_lh_id}")

    # Warehouse rebinding
    wh_info = dependencies.get("warehouse", {})
    src_wh_id = wh_info.get("default_warehouse")
    if src_wh_id:
        src_wh_name = client.resolve_item_name(source_ws_id, src_wh_id, "Warehouse")
        if src_wh_name:
            tgt_wh_id = client.resolve_item_id(target_ws_id, src_wh_name, "Warehouse")
            if tgt_wh_id and tgt_wh_id != src_wh_id:
                wh_info["default_warehouse"] = tgt_wh_id
                for kw in wh_info.get("known_warehouses", []):
                    if kw.get("id") == src_wh_id:
                        kw["id"] = tgt_wh_id
                changed = True
                print(f"    Warehouse '{src_wh_name}' -> {tgt_wh_id}")

    if changed:
        return json.dumps(nb_json, indent=1), True
    return content_str, False


def _rebind_notebook_py(content_str, client, source_ws_id, target_ws_id, nb_name):
    """Rebind lakehouse/warehouse refs in .py META comments."""
    changed = False
    lines = content_str.split("\n")

    # Pattern: # META "default_lakehouse": "uuid"
    lh_pattern = re.compile(r'(#\s*META\s+"default_lakehouse":\s*)"([0-9a-fA-F-]{36})"')
    lh_ws_pattern = re.compile(
        r'(#\s*META\s+"default_lakehouse_workspace_id":\s*)"([0-9a-fA-F-]{36})"'
    )
    wh_pattern = re.compile(r'(#\s*META\s+"default_warehouse":\s*)"([0-9a-fA-F-]{36})"')

    # First pass: find lakehouse/warehouse names from source IDs
    lh_name_cache = {}
    wh_name_cache = {}

    for line in lines:
        m = lh_pattern.search(line)
        if m:
            src_id = m.group(2)
            name = client.resolve_item_name(source_ws_id, src_id, "Lakehouse")
            if name:
                lh_name_cache[src_id] = name

        m = wh_pattern.search(line)
        if m:
            src_id = m.group(2)
            name = client.resolve_item_name(source_ws_id, src_id, "Warehouse")
            if name:
                wh_name_cache[src_id] = name

    # Second pass: replace IDs
    new_lines = []
    for line in lines:
        # Lakehouse ID
        m = lh_pattern.search(line)
        if m:
            src_id = m.group(2)
            lh_name = lh_name_cache.get(src_id)
            if lh_name:
                tgt_id = client.resolve_item_id(target_ws_id, lh_name, "Lakehouse")
                if tgt_id and tgt_id != src_id:
                    line = line.replace(src_id, tgt_id)
                    changed = True
                    print(f"    Lakehouse '{lh_name}' -> {tgt_id}")

        # Lakehouse workspace ID
        m = lh_ws_pattern.search(line)
        if m:
            src_ws = m.group(2)
            if src_ws != target_ws_id:
                line = line.replace(src_ws, target_ws_id)
                changed = True

        # Warehouse ID
        m = wh_pattern.search(line)
        if m:
            src_id = m.group(2)
            wh_name = wh_name_cache.get(src_id)
            if wh_name:
                tgt_id = client.resolve_item_id(target_ws_id, wh_name, "Warehouse")
                if tgt_id and tgt_id != src_id:
                    line = line.replace(src_id, tgt_id)
                    changed = True
                    print(f"    Warehouse '{wh_name}' -> {tgt_id}")

        new_lines.append(line)

    if changed:
        return "\n".join(new_lines), True
    return content_str, False


# ============================================================================
# 3. Semantic Model Rebinding
# ============================================================================

def rebind_semantic_models(client, source_ws_id, target_ws_id, connection_mapping, target_stage):
    """
    Update Semantic Model definitions in the target workspace:
    - Direct Lake: update the lakehouse SQL endpoint connection
    - Import/DirectQuery: update M expressions with mapped connections
    """
    print("\n=== Post-Deployment: Semantic Model Rebinding ===")

    models = client.list_items(target_ws_id, "SemanticModel")
    if not models:
        print("  No Semantic Models found — skipping")
        return

    for model in models:
        name = model["displayName"]
        model_id = model["id"]

        # Skip default semantic models (auto-generated by Lakehouse/Warehouse)
        if _is_default_semantic_model(name, client, target_ws_id):
            print(f"  Skipping default model: {name}")
            continue

        print(f"  Processing semantic model: {name}")

        # Try Direct Lake first
        if _try_rebind_direct_lake(client, target_ws_id, model_id, name):
            continue

        # Fall back to BIM/M expression update
        _rebind_semantic_model_bim(
            client, source_ws_id, target_ws_id, model_id, name,
            connection_mapping, target_stage
        )


def _is_default_semantic_model(model_name, client, workspace_id):
    """Check if this is an auto-generated default semantic model."""
    # Default models share their name with a Lakehouse or Warehouse
    lakehouses = client.list_items(workspace_id, "Lakehouse")
    for lh in lakehouses:
        if lh["displayName"] == model_name:
            return True
    warehouses = client.list_items(workspace_id, "Warehouse")
    for wh in warehouses:
        if wh["displayName"] == model_name:
            return True
    return False


def _try_rebind_direct_lake(client, target_ws_id, model_id, model_name):
    """
    Attempt to rebind a Direct Lake semantic model to its lakehouse
    in the target workspace. Returns True if handled.
    """
    try:
        # Get the model definition in TMDL format via semantic model-specific API
        parts = client.get_semantic_model_definition(target_ws_id, model_id)
        if not parts:
            return False

        # Check ALL .tmdl files for directLake mode (it lives in table files, not model.tmdl)
        is_direct_lake = False
        for part in parts:
            if part.get("path", "").endswith(".tmdl"):
                payload = base64.b64decode(part["payload"]).decode("utf-8")
                if "mode: directlake" in payload.lower():
                    is_direct_lake = True
                    break
        if not is_direct_lake:
            return False

        # Parse expressions.tmdl to find the data source connection
        expr_text = None
        for part in parts:
            if part.get("path", "").endswith("expressions.tmdl"):
                expr_text = base64.b64decode(part["payload"]).decode("utf-8")
                break
        if not expr_text:
            return False

        # Pattern 1: Warehouse — Sql.Database("server", "databaseId")
        wh_match = re.search(
            r'Sql\.Database\("([^"]+)"\s*,\s*"([^"]+)"\)',
            expr_text,
        )
        # Pattern 2: Lakehouse — DatabaseName = "..." / ServerName = "..."
        lh_match = re.search(r'DatabaseName\s*=\s*"([^"]+)"', expr_text)

        if wh_match:
            # Warehouse-connected Direct Lake model
            old_server = wh_match.group(1)
            old_db_id = wh_match.group(2)

            # Find the warehouse in the target workspace by matching the source DB ID
            # First, find the warehouse name from the source workspace
            wh_name = client.resolve_item_name(target_ws_id, old_db_id, "Warehouse")
            if not wh_name:
                # The ID doesn't match target — find by checking all warehouses
                # Try to identify the warehouse by name from source workspace
                source_wh_name = None
                src_warehouses = client.list_items(target_ws_id, "Warehouse")
                if len(src_warehouses) == 1:
                    source_wh_name = src_warehouses[0]["displayName"]
                else:
                    # Try to match from source workspace items
                    print(f"    [WARN] Multiple warehouses in target — cannot auto-detect which to rebind")
                    return False
                wh_name = source_wh_name

            tgt_wh_id = client.resolve_item_id(target_ws_id, wh_name, "Warehouse")
            if not tgt_wh_id:
                print(f"    [WARN] Warehouse '{wh_name}' not found in target — skipping")
                return False

            # Get the warehouse SQL endpoint
            resp = client.get(f"workspaces/{target_ws_id}/warehouses/{tgt_wh_id}")
            if resp.status_code != 200:
                print(f"    [ERROR] Could not get warehouse properties: {resp.status_code}")
                return False
            wh_props = resp.json().get("properties", {})
            sql_endpoint = wh_props.get("connectionString", "")
            if not sql_endpoint:
                print(f"    [WARN] No SQL endpoint for warehouse '{wh_name}'")
                return False

            # Update expressions.tmdl: Sql.Database("newServer", "newDbId")
            new_expr = expr_text.replace(
                f'Sql.Database("{old_server}", "{old_db_id}")',
                f'Sql.Database("{sql_endpoint}", "{tgt_wh_id}")',
            )
            if new_expr != expr_text:
                for part in parts:
                    if part.get("path", "").endswith("expressions.tmdl"):
                        part["payload"] = base64.b64encode(new_expr.encode("utf-8")).decode("utf-8")
                        part["payloadType"] = "InlineBase64"
                resp = client.update_semantic_model_definition(target_ws_id, model_id, parts)
                if resp.status_code in (200, 202):
                    print(f"    Direct Lake -> Warehouse '{wh_name}' rebound")
                else:
                    print(f"    [ERROR] Direct Lake update failed: {resp.status_code} {resp.text[:200]}")
            else:
                print(f"    Direct Lake already pointing to target warehouse")
            return True

        elif lh_match:
            # Lakehouse-connected Direct Lake model
            lakehouse_name = lh_match.group(1)

            tgt_lh_id = client.resolve_item_id(target_ws_id, lakehouse_name, "Lakehouse")
            if not tgt_lh_id:
                print(f"    [WARN] Lakehouse '{lakehouse_name}' not found in target — skipping")
                return False

            resp = client.get(f"workspaces/{target_ws_id}/lakehouses/{tgt_lh_id}")
            if resp.status_code != 200:
                return False
            lh_props = resp.json().get("properties", {})
            sql_endpoint = lh_props.get("sqlEndpointProperties", {}).get("connectionString", "")
            if not sql_endpoint:
                print(f"    [WARN] No SQL endpoint for lakehouse '{lakehouse_name}'")
                return False

            updated = False
            for part in parts:
                if part.get("path", "").endswith("expressions.tmdl"):
                    expr = base64.b64decode(part["payload"]).decode("utf-8")
                    new_expr = re.sub(
                        r'(ServerName\s*=\s*)"[^"]*"',
                        f'\\1"{sql_endpoint}"',
                        expr,
                    )
                    new_expr = re.sub(
                        r'(DatabaseName\s*=\s*)"[^"]*"',
                        f'\\1"{lakehouse_name}"',
                        new_expr,
                    )
                    if new_expr != expr:
                        part["payload"] = base64.b64encode(new_expr.encode("utf-8")).decode("utf-8")
                        part["payloadType"] = "InlineBase64"
                        updated = True

            if updated:
                resp = client.update_semantic_model_definition(target_ws_id, model_id, parts)
                if resp.status_code in (200, 202):
                    print(f"    Direct Lake -> Lakehouse '{lakehouse_name}' rebound")
                else:
                    print(f"    [ERROR] Direct Lake update failed: {resp.status_code}")
            return True

        return False

    except Exception as e:
        print(f"    [INFO] Not a Direct Lake model or check failed: {e}")
        return False


def _rebind_semantic_model_bim(client, source_ws_id, target_ws_id, model_id, model_name,
                                connection_mapping, target_stage):
    """Update M expressions in TMDL definition files using connection mapping."""
    if not connection_mapping:
        print(f"    No connection mapping — skipping M expression update")
        return

    parts = client.get_semantic_model_definition(target_ws_id, model_id)
    if not parts:
        print(f"    [WARN] Could not get definition — skipping")
        return

    updated = False
    for part in parts:
        path = part.get("path", "")
        # Process TMDL expression files and table definition files
        if not (path.endswith(".tmdl") or path.endswith(".bim")):
            continue

        payload_b64 = part.get("payload", "")
        try:
            content = base64.b64decode(payload_b64).decode("utf-8")
        except Exception:
            continue

        new_content = _replace_m_expressions(content, connection_mapping, target_stage)
        if new_content != content:
            part["payload"] = base64.b64encode(new_content.encode("utf-8")).decode("utf-8")
            part["payloadType"] = "InlineBase64"
            updated = True

    if updated:
        resp = client.update_semantic_model_definition(target_ws_id, model_id, parts)
        if resp.status_code in (200, 202):
            print(f"    M expressions updated")
        else:
            print(f"    [ERROR] Update failed: {resp.status_code} {resp.text}")
    else:
        print(f"    No M expression changes needed")


def _replace_m_expressions(content, connection_mapping, target_stage):
    """Replace M expression source lines using the connection mapping."""
    lines = content.split("\n")
    new_lines = []
    for line in lines:
        # Match pattern: Source = <expression>,
        m = re.match(r'(\s*)Source\s*=\s*(.*),\s*$', line)
        if m:
            indent = m.group(1)
            dev_expr = m.group(2).strip()
            target_expr = _find_connection_expr(dev_expr, connection_mapping, target_stage)
            if target_expr and target_expr != dev_expr:
                line = f"{indent}Source = {target_expr},"
                print(f"    M expression: {dev_expr[:60]}... -> {target_expr[:60]}...")
        new_lines.append(line)
    return "\n".join(new_lines)


def _find_connection_expr(dev_expr, connection_mapping, target_stage):
    """Find the target connection expression from the mapping."""
    for mapping in connection_mapping:
        stage1 = mapping.get("ConnectionStage1", "")
        if stage1 == dev_expr:
            target_col = f"ConnectionStage{target_stage}"
            return mapping.get(target_col, dev_expr) or dev_expr
    return dev_expr


# ============================================================================
# 4. Report Rebinding
# ============================================================================

def rebind_reports(client, target_ws_id):
    """
    Rebind reports to their semantic models in the target workspace.
    Reports reference semantic models by ID; after deployment, the IDs
    in the target workspace differ from the source.
    """
    print("\n=== Post-Deployment: Report Rebinding ===")

    reports = client.list_items(target_ws_id, "Report")
    if not reports:
        print("  No Reports found — skipping")
        return

    models = client.list_items(target_ws_id, "SemanticModel")
    model_map = {m["id"]: m["displayName"] for m in models}
    model_name_to_id = {m["displayName"]: m["id"] for m in models}

    for report in reports:
        name = report["displayName"]
        report_id = report["id"]
        print(f"  Processing report: {name}")

        parts = client.get_item_definition(target_ws_id, report_id)
        if not parts:
            print(f"    [WARN] Could not get definition — skipping")
            continue

        updated = False
        for part in parts:
            if part.get("path") != "definition.pbir":
                continue

            payload_b64 = part.get("payload", "")
            try:
                content = json.loads(base64.b64decode(payload_b64).decode("utf-8"))
            except Exception:
                continue

            data_model = content.get("datasetReference", {})
            by_path = data_model.get("byPath", {})
            by_connection = data_model.get("byConnection", {})

            # byPath references (same workspace) — update path if needed
            if by_path and by_path.get("path"):
                # These use relative paths like /MyModel.SemanticModel
                # fabric-cicd already handles same-workspace byPath refs
                pass

            # byConnection references — update dataset ID
            if by_connection:
                ds_id = by_connection.get("connectionString")
                ds_name = by_connection.get("pbiModelDatabaseName")
                if ds_name and ds_name in model_name_to_id:
                    new_ds_id = model_name_to_id[ds_name]
                    if ds_id != new_ds_id:
                        by_connection["connectionString"] = new_ds_id
                        new_payload = base64.b64encode(
                            json.dumps(content).encode("utf-8")
                        ).decode("utf-8")
                        part["payload"] = new_payload
                        part["payloadType"] = "InlineBase64"
                        updated = True
                        print(f"    Report -> SemanticModel '{ds_name}' rebound")

        if updated:
            resp = client.update_item_definition(target_ws_id, report_id, parts)
            if resp.status_code in (200, 202):
                print(f"    Updated successfully")
            else:
                print(f"    [ERROR] Update failed: {resp.status_code} {resp.text}")
        else:
            print(f"    No changes needed")


# ============================================================================
# Workspace Resolution
# ============================================================================

_UUID_PATTERN = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


def _resolve_workspace(client, name_or_id):
    """Accept either a workspace ID (GUID) or display name and return the ID."""
    if _UUID_PATTERN.match(name_or_id):
        return name_or_id
    # Look up by name
    resp = client.get("workspaces")
    resp.raise_for_status()
    for ws in resp.json().get("value", []):
        if ws["displayName"] == name_or_id:
            return ws["id"]
    raise ValueError(f"Workspace '{name_or_id}' not found")


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Post-deployment operations for Fabric CI/CD")
    parser.add_argument("--aztenantid", required=True, help="Azure tenant ID")
    parser.add_argument("--azclientid", required=True, help="Service Principal client ID")
    parser.add_argument("--azspsecret", required=True, help="Service Principal secret")
    parser.add_argument("--source_workspace_id", required=True,
                        help="Source workspace ID or name")
    parser.add_argument("--target_workspace_id", required=True,
                        help="Target workspace ID or name")
    parser.add_argument("--target_stage", required=False, default="2",
                        help="Target stage number for connection mapping (2, 3, etc.)")
    parser.add_argument("--mapping_connections", required=False, default="",
                        help="Path to mapping_connections.json file")
    args = parser.parse_args()

    print("=" * 60)
    print(" Fabric CI/CD Post-Deployment Operations")
    print("=" * 60)

    # Authenticate
    credential = ClientSecretCredential(
        tenant_id=args.aztenantid,
        client_id=args.azclientid,
        client_secret=args.azspsecret,
    )
    client = FabricClient(credential)

    # Resolve workspace names to IDs if needed
    source_ws_id = _resolve_workspace(client, args.source_workspace_id)
    target_ws_id = _resolve_workspace(client, args.target_workspace_id)

    print(f"  Source workspace: {source_ws_id}")
    print(f"  Target workspace: {target_ws_id}")
    print(f"  Target stage:     {args.target_stage}")

    # Load connection mapping if provided
    connection_mapping = []
    if args.mapping_connections:
        try:
            with open(args.mapping_connections, "r") as f:
                connection_mapping = json.load(f)
            print(f"  Loaded {len(connection_mapping)} connection mappings")
        except FileNotFoundError:
            print(f"  [WARN] Mapping file not found: {args.mapping_connections}")
        except json.JSONDecodeError as e:
            print(f"  [WARN] Invalid JSON in mapping file: {e}")

    # Execute all post-deployment operations
    rebind_data_pipelines(client, source_ws_id, target_ws_id,
                          connection_mapping, args.target_stage)
    rebind_notebooks(client, source_ws_id, target_ws_id)
    rebind_semantic_models(client, source_ws_id, target_ws_id,
                           connection_mapping, args.target_stage)
    rebind_reports(client, target_ws_id)

    print("\n" + "=" * 60)
    print(" Post-deployment complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
