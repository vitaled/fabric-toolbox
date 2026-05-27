# Demo Guide: Git-Based Deployments Using Build Environments

This guide walks through setting a live demo of **Option 2** — code-first CI/CD for Microsoft Fabric using the [`fabric-cicd`](https://pypi.org/project/fabric-cicd/) Python library, Azure DevOps pipelines, and a single multi-stage pipeline with approval gates.

---

## What This Demo Shows

The demo illustrates an end-to-end CI/CD workflow for Microsoft Fabric where:

1. **Git is the single source of truth** — Fabric item definitions (Notebooks, Lakehouses, Warehouses, Pipelines, Reports, Semantic Models) are stored as files in an Azure DevOps Git repository.
2. **Workspaces are NOT Git-connected** — unlike direct Git integration, all deployments to Fabric workspaces originate from an Azure DevOps pipeline. This avoids workspace lock-in and allows centralized control.
3. **Stage-to-workspace mapping** — a single pipeline triggered on `main` deploys sequentially to each environment (Dev → Test) using workspace names from the variable group (`mainWorkspaceName`, `testWorkspaceName`).
4. **Service Principal authentication** — the pipeline authenticates using an Entra ID Service Principal with credentials stored in Azure Key Vault.
5. **Approval gates** — promotion between pipeline stages (e.g., Dev → Test) requires manual approval via Azure DevOps environment checks.
6. **fabric-cicd library** — the open-source Python library handles creating, updating, and removing Fabric items via the Fabric REST API.

### Architecture

```
Feature Branch      PR        main branch              Pipeline (multi-stage)
+-----------+    +-------+    +-----------+    +-------------------------------------+
| Edit item | -> | PR    | -> | Merge     | -> | Stage 1: Deploy to Dev (Demo-Dev)   |
| definition|    | review|    |           |    |   - fabric-cicd publish_all_items   |
+-----------+    +-------+    +-----------+    |   - post-deployment rebinding       |
                                               |   - DACPAC build & publish (opt.)   |
                                               +------------------+------------------+
                                                                  |
                                                                  v (approval gate)
                                               +------------------+------------------+
                                               | Stage 2: Deploy to Test (Demo-Test) |
                                               |   - fabric-cicd publish_all_items   |
                                               |   - post-deployment rebinding       |
                                               |   - DACPAC build & publish (opt.)   |
                                               +-------------------------------------+

Extract-Lakehouse-Schema pipeline (manual trigger)
+--------------------------------------------------+
| Extracts SQL schemas from Lakehouse SQL endpoints |
| → commits .sqlproj files to lakehouse-schema/    |
+--------------------------------------------------+
```

### Item Types Deployed

The pipeline deploys the following Fabric item types:

| Item Type | Description |
|---|---|
| Notebook | PySpark / Python notebooks |
| DataPipeline | Data Factory pipelines |
| Lakehouse | Lakehouse with tables and shortcuts |
| Warehouse | SQL data warehouse (T-SQL) |
| SemanticModel | Power BI semantic models (TMDL) |
| Report | Power BI reports (PBIR) |

---

## Prerequisites

Before running the setup, ensure you have:

| Requirement | How to Verify |
|---|---|
| **Azure CLI** (v2.50+) | `az --version` |
| **Azure DevOps CLI extension** | `az extension show --name azure-devops` (install: `az extension add --name azure-devops`) |
| **Git CLI** | `git --version` |
| **Python 3.10+** | `python --version` |
| **Azure subscription** | With permissions to create Resource Groups, Key Vaults, Service Principals |
| **Azure DevOps organization** | An existing org (e.g., `https://dev.azure.com/myorg`) |
| **Azure DevOps PAT** | Personal Access Token with **Full access** scope |
| **Fabric capacity** | An F or P SKU capacity (or Trial) already provisioned |
| **Fabric admin access** | Ability to create workspaces and assign capacity |

---

## Setup Instructions

### Automated Setup (setup-demo.ps1)

The `setup-demo.ps1` script automates the majority of the setup across three phases. It pauses at manual steps that require portal interaction.

#### Running the Script

```powershell
.\setup-demo.ps1 `
    -SubscriptionId "<your-azure-subscription-id>" `
    -Location "eastus" `
    -AzDoOrg "https://dev.azure.com/<your-org>" `
    -AzDoPat "<your-pat>" `
    -ResourcePostfix "demo1"   # optional — random 4-digit number if omitted
```

> **Naming convention:** All resources are suffixed with the `-{postfix}` value (e.g., `rg-fabric-cicd-demo1`, `Demo-Dev-demo1`). This allows multiple independent demo environments in the same subscription/organization. If `-ResourcePostfix` is omitted, a random 4-digit number is used.

#### Phase 1: Azure & Entra ID

The script creates:

| Resource | Name | Purpose |
|---|---|---|
| Resource Group | `rg-fabric-cicd-{postfix}` | Container for all Azure resources |
| Service Principal | `sp-fabric-cicd-{postfix}` | Pipeline identity for Fabric API access |
| Key Vault | `kv-fab-cicd-{postfix}` | Stores SP credentials (tenant ID, client ID, secret) |

The Key Vault contains three secrets:
- `aztenantid` — Entra ID tenant ID
- `azclientid` — Service Principal app/client ID
- `azspsecret` — Service Principal client secret

The Service Principal is granted `Key Vault Secrets User` role on the vault.

> **Note:** The Key Vault is created with RBAC authorization by default. If Azure DevOps variable group linking fails with a permissions warning, switch to access policies:
> ```bash
> az keyvault update --name <kv-name> --resource-group rg-fabric-cicd-{postfix} --enable-rbac-authorization false
> az keyvault set-policy --name <kv-name> --spn <sp-client-id> --secret-permissions get list
> az keyvault set-policy --name <kv-name> --object-id <your-user-oid> --secret-permissions get list set delete
> ```

#### Phase 2: Fabric Workspaces

The script creates:

| Workspace | Pipeline Stage | Purpose |
|---|---|---|
| `Demo-Dev-{postfix}` | Stage 1 (auto on merge to `main`) | Development / integration environment |
| `Demo-Test-{postfix}` | Stage 2 (approval gate) | Test / staging environment |

Both workspaces are:
- Assigned to a Fabric capacity (you select which one during setup)
- Granted the Service Principal as **Admin** (required for `fabric-cicd` deployments)

##### Manual Step: Seed Demo Content

After workspace creation, you must seed initial content in `Demo-Dev-{postfix}` via the Fabric portal:

1. Open [app.fabric.microsoft.com](https://app.fabric.microsoft.com)
2. Navigate to the `Demo-Dev-{postfix}` workspace
3. Create the following items:
   - **Notebook** named `HelloWorld` — add a cell: `print("Hello from Dev!")`
   - **Lakehouse** named `DemoLakehouse`
   - **Warehouse** named `DemoWarehouse` — run: `CREATE TABLE dbo.DemoTable (Id INT, Name VARCHAR(100))`
   - *(Optional)* **Semantic Model** and **Report** connected to the Lakehouse/Warehouse
   - *(Optional)* **Data Pipeline** with a placeholder activity

#### Phase 3: Azure DevOps

The script creates:

| Resource | Name | Purpose |
|---|---|---|
| Project | `FabricCICD-{postfix}` | DevOps project (private) |
| Repository | `fabric-demo-repo` | Git repo initialized from `fabric-cicd` source |
| Branch:  | `main` | Triggers multi-stage pipeline (deploys to Dev, then Test) |
| Pipeline | `Deploy-To-Fabric` | Multi-stage YAML pipeline using `Deploy-To-Fabric.yml` |
| Environments | `dev`, `test` | Pipeline environments (approval gate on `test`) |

##### Manual Step: ARM Service Connection

Create a service connection so the pipeline can read Key Vault secrets:

1. Go to **Project Settings → Service connections**
2. Click **New service connection → Azure Resource Manager**
3. Choose **Service principal (manual)** and use the SP credentials from Phase 1
4. Scope to your subscription / resource group `rg-fabric-cicd-{postfix}`
5. Name it: `azure-kv-connection`

##### Manual Step: Git Sync from Fabric

This one-time sync exports item definitions from the Fabric workspace to the Git repo:

1. Open [app.fabric.microsoft.com](https://app.fabric.microsoft.com)
2. Go to `Demo-Dev-{postfix}` → **Settings → Git integration**
3. Connect to:
   - Organization: `<your-org>`
   - Project: `FabricCICD-{postfix}`
   - Repository: `fabric-demo-repo`
   - Branch: `main`
   - Git folder: `fabric/`
4. Click **Connect and sync** — all item definitions are committed to `main`
5. **Disconnect** the workspace from Git after sync completes

> **Important:** The workspace must be disconnected after the initial sync. From this point forward, all deployments flow from Git → Pipeline → Workspace.

##### Manual Step: Variable Groups

Create two variable groups under **Pipelines → Library**:

**1. `Fabric_Deployment_Group_S`** (Key Vault linked)
- Toggle ON **Link secrets from an Azure key vault as variables**
- Service connection: `azure-kv-connection`
- Key vault: `kv-fab-cicd-{postfix}`
- Add secrets: `aztenantid`, `azclientid`, `azspsecret`

**2. `Fabric_Deployment_Group_NS`** (plain variables)
- `mainWorkspaceName` = `Demo-Dev-{postfix}`
- `testWorkspaceName` = `Demo-Test-{postfix}`
- `gitDirectory` = `fabric`

> **Note:** A Key Vault-linked variable group can only contain secrets from the vault. Non-secret variables must go in a separate group.

##### Manual Step: Pipeline Permissions & Approvals

1. **Grant pipeline access** to both variable groups:
   - Go to each variable group → **Pipeline permissions** → Add `Deploy-To-Fabric`

2. **Add approval gate** on the `test` environment:
   - Go to **Pipelines → Environments → test → Approvals and checks**
   - Add yourself as approver

---

## Validation

Before running the live demo, validate the pipeline:

1. Go to **Pipelines** in Azure DevOps
2. Run the `Deploy-To-Fabric` pipeline manually on the `main` branch
3. Verify it completes successfully — check the Fabric portal to confirm items are in `Demo-Dev-{postfix}`

Common issues:
- **"PrincipalNotFound"** — the SP needs a few minutes to propagate to Fabric after creation. Retry the workspace role assignment.
- **"Workspace not found"** — verify `mainWorkspaceName` / `testWorkspaceName` in the `_NS` variable group match the actual workspace names exactly.
- **Key Vault "Forbidden"** — ensure the SP has `Get, List` permissions via RBAC role or access policy.

---

## Running the Live Demo

### Using the Demo Script (demo-script.ps1)

The `demo-script.ps1` provides a guided, interactive walkthrough with pauses at each stage.

**Option A — Use the config file generated by setup** (recommended):

```powershell
.\demo-script.ps1 -ConfigFile .\demo-config.json
```

**Option B — Pass parameters explicitly:**

```powershell
.\demo-script.ps1 `
    -RepoPath "C:\path\to\cloned\fabric-demo-repo" `
    -AzDoOrg "https://dev.azure.com/<your-org>" `
    -AzDoProject "FabricCICD-{postfix}"
```

> Individual parameters override values from the config file when both are provided.

### Demo Flow

#### Step 1 — Explain the Architecture

The script displays the architecture diagram showing the flow from feature branch → PR → merge → multi-stage pipeline → workspaces. Key talking points:

- Workspaces are **not Git-connected** — the pipeline is the single deployment mechanism
- The `fabric-cicd` library handles all Fabric REST API calls (create, update, delete items)
- A **single pipeline** with sequential stages deploys to Dev, then Test (with approval gate)
- Post-deployment automatically rebinds connections, notebooks, semantic models, and reports
- Secrets are stored securely in Azure Key Vault

#### Step 2 — Create a Feature Branch

The script:
1. Checks out `main` and pulls latest
2. Creates a feature branch (e.g., `feature/update-notebook-0513-1430`)
3. Modifies the `HelloWorld` notebook by appending a new Python cell:
   ```python
   # Demo change added on 2026-05-13 14:30:00
   print('CI/CD demo - deployed automatically!')
   ```

This is a visible change that can be verified in the Fabric portal after deployment.

#### Step 3 — Commit, Push, and Create PR

The script:
1. Commits the change to the feature branch
2. Pushes to the remote
3. Creates a Pull Request targeting `main` via the Azure DevOps CLI

#### Step 4 — Merge PR & Watch Pipeline Deploy to Dev and Test

**Manual steps** (great for showing the audience):

**Stage 1 — Deploy to Dev:**
1. Open the PR in Azure DevOps — show the diff (the notebook change)
2. Approve and complete the merge
3. Navigate to **Pipelines** — the `Deploy-To-Fabric` pipeline triggers automatically
4. Walk through the pipeline logs:
   - Python 3.12 setup
   - `pip install fabric-cicd`
   - SP authentication, workspace ID lookup, `publish_all_items()`
5. After deploy completes, post-deployment runs automatically:
   - Rebinds notebooks (lakehouse/warehouse references)
   - Rebinds data pipelines (connection IDs, linked services, activity refs)
   - Rebinds semantic models (Direct Lake or M expression connections)
   - Rebinds reports (semantic model references)
6. Open [app.fabric.microsoft.com](https://app.fabric.microsoft.com) → `Demo-Dev-{postfix}` workspace → open the `HelloWorld` notebook → **the new cell is there**

**Stage 2 — Deploy to Test (with approval gate):**
7. The **approval gate fires** — approve it in the pipeline run
8. Same process: `fabric-cicd` deploys items, then post-deployment rebinds references
9. Open `Demo-Test-{postfix}` workspace in Fabric portal → verify the same changes are deployed

#### Demo Complete

The demo has shown:
1. Feature branch development with PR review
2. Merge to `main` triggers a **single multi-stage pipeline**
3. Stage 1: automated deployment to Dev + post-deployment rebinding
4. Stage 2: approval gate → deployment to Test + post-deployment rebinding
5. All 6 item types (including Warehouse) handled by `fabric-cicd`
6. SQL schema deployment via DACPAC (Warehouse + Lakehouse SQL endpoints)

---

## Key Files Reference

| File | Purpose |
|---|---|
| [`setup-demo.ps1`](setup-demo.ps1) | Automated setup script — creates all Azure, Fabric, and DevOps resources |
| [`demo-script.ps1`](demo-script.ps1) | Interactive live demo walkthrough |
| `demo-config.json` | Generated by `setup-demo.ps1` — contains all dynamic values for `demo-script.ps1` |
| [`AzDO/Deploy-To-Fabric.yml`](AzDO/Deploy-To-Fabric.yml) | Azure DevOps YAML pipeline definition |
| [`AzDO/Extract-Lakehouse-Schema.yml`](AzDO/Extract-Lakehouse-Schema.yml) | Pipeline to extract Lakehouse SQL schemas from Fabric |
| [`AzDO/scripts/deploy/deploy-to-fabric.py`](AzDO/scripts/deploy/deploy-to-fabric.py) | Python deployment script using `fabric-cicd` |
| [`AzDO/scripts/deploy/post_deployment.py`](AzDO/scripts/deploy/post_deployment.py) | Post-deployment rebinding script (pipelines, notebooks, semantic models, reports) |
| [`AzDO/scripts/deploy/extract-lakehouse-schema.ps1`](AzDO/scripts/deploy/extract-lakehouse-schema.ps1) | PowerShell script for Lakehouse schema extraction |
| [`AzDO/scripts/deploy/mapping_connections.template.json`](AzDO/scripts/deploy/mapping_connections.template.json) | Template for connection mapping between environments |
| [`readme.md`](readme.md) | Original accelerator documentation |

---

## Post-Deployment Operations

After `fabric-cicd` publishes item definitions to a workspace, certain references remain hardcoded to the source (Dev) workspace — connection strings, item IDs, workspace IDs, etc. The **post-deployment step** automatically rebinds these references to the target workspace.

### What Gets Rebinded

| Operation | What it does |
|---|---|
| **Data Pipeline rebinding** | Remaps connection IDs, Warehouse/Lakehouse linked services, Notebook/Pipeline/SemanticModel activity references |
| **Notebook rebinding** | Updates default Lakehouse and Warehouse dependencies to target workspace items (both `.ipynb` and `.py` formats) |
| **Semantic Model rebinding** | Direct Lake: updates SQL endpoint connection. Import/DirectQuery: updates M expressions using connection mapping |
| **Report rebinding** | Updates `definition.pbir` references to point to semantic models in the target workspace |

### Pipeline Parameters

The pipeline YAML exposes two new parameters:

| Parameter | Default | Description |
|---|---|---|
| `run_post_deployment` | `true` | Toggle post-deployment on/off |
| `mapping_connections_file` | `scripts/deploy/mapping_connections.json` | Path to the connection mapping JSON file |

### Connection Mapping File

If your Fabric items use different connections per environment (e.g., different SQL endpoints, storage accounts), create a `mapping_connections.json` file based on the template:

```json
[
    {
        "ConnectionStage1": "dev-connection-guid-or-m-expression",
        "ConnectionStage2": "test-connection-guid-or-m-expression",
        "ConnectionStage3": "prod-connection-guid-or-m-expression"
    }
]
```

- **Stage 1** = Dev workspace (source)
- **Stage 2** = Test workspace
- **Stage 3** = Production workspace

Each entry maps a connection ID or M expression from Stage 1 to the equivalent in higher stages. The script uses `ConnectionStageN` where N is the `--target_stage` argument.

If you don't use environment-specific connections, the mapping file can be empty (`[]`) or omitted — notebook, report, and pipeline rebinding still work without it.

### How It Works in the Pipeline

Each stage in `Deploy-To-Fabric.yml` now has an optional post-deployment step:

```
Stage 1: Deploy to Dev → Post-Deployment: Rebind Dev (stage 1)
Stage 2: Deploy to Test (approval gate) → Post-Deployment: Rebind Test (stage 2)
```

The post-deployment script:
1. Authenticates with the same Service Principal used for deployment
2. Resolves workspace names to IDs (accepts both names and GUIDs)
3. Lists all items of each type in the target workspace
4. For each item, fetches its definition, rebinds references, and updates it via the Fabric API
5. Skips items that don't need changes

---

## SQL Schema Deployment (DACPAC)

In addition to deploying Fabric item definitions via `fabric-cicd`, the pipeline can also build and deploy SQL schemas to Warehouses and Lakehouses using `.sqlproj` files and SqlPackage. This approach is based on the [Git-based deployment with sqlproj schemas](../Git-based-deployment-with-sqlproj-schemas/) accelerator.

### How It Works

```
lakehouse-schema/            .sqlproj files extracted from Lakehouse SQL endpoints
    └── DemoLakehouse/
        ├── DemoLakehouse.sqlproj
        ├── Tables/
        ├── Views/
        └── StoredProcedures/

fabric/                      Fabric item definitions (synced from workspace)
    └── DemoWarehouse.Warehouse/
        ├── .platform
        ├── DemoWarehouse.sqlproj   (optional — for warehouse schema CI/CD)
        └── dbo/
```

The Deploy-To-Fabric pipeline includes optional DACPAC steps (controlled by the `deploy_sql_schemas` parameter):

1. **Build Lakehouse DACPACs** — Discovers `.sqlproj` files under `lakehouse-schema/`, auto-detects cross-database dependencies via SQL code scanning, and builds in topological order.
2. **Build Warehouse DACPAC** — Builds `.sqlproj` files under `fabric/`, injecting `ArtifactReference` elements pointing to the built Lakehouse DACPACs for cross-database resolution.
3. **Publish Warehouse Schema** — Uses SqlPackage to publish the Warehouse DACPAC to the target Fabric Warehouse.
4. **Publish Lakehouse Schemas** — Uses SqlPackage to publish Lakehouse DACPACs to the target SQL endpoints (excludes Tables — only views, stored procedures, and functions are deployed).

### Extract-Lakehouse-Schema Pipeline

A separate pipeline (`Extract-Lakehouse-Schema.yml`) extracts SQL schemas from Fabric Lakehouse SQL endpoints and commits them as `.sqlproj` files to the repo:

1. Connects to the Fabric workspace using the Service Principal
2. Discovers all Lakehouse items and their SQL endpoints
3. Uses SqlPackage to extract schemas (views, stored procedures, functions, tables)
4. Organizes extracted files into `.sqlproj` projects with proper folder structure
5. Detects cross-database references and adds `ArtifactReference` elements
6. Commits the extracted schemas to Git

**Run the extraction pipeline** before the first deployment to populate the `lakehouse-schema/` folder:

1. Go to **Pipelines** in Azure DevOps
2. Run **Extract-Lakehouse-Schema** manually
3. After it completes, `lakehouse-schema/` will contain `.sqlproj` files for each Lakehouse
4. Subsequent runs of **Deploy-To-Fabric** will automatically build and deploy these schemas

### Pipeline Parameters

| Parameter | Default | Description |
|---|---|---|
| `deploy_sql_schemas` | `true` | Toggle DACPAC build & publish on/off |
| `workspace_name` (extract pipeline) | *(empty — uses `mainWorkspaceName`)* | Workspace to extract schemas from |
| `create_branch` (extract pipeline) | `false` | Create a new branch for schema changes |

### Prerequisites on the Pipeline Agent

The DACPAC steps require:
- **.NET SDK 8.x** — installed automatically via `UseDotNet@2` task
- **SqlPackage** — installed automatically via `dotnet tool install`

### Cross-Database References

If your Warehouse queries reference Lakehouse tables (e.g., `SELECT * FROM [DemoLakehouse].[dbo].[MyTable]`), the build system automatically:

1. Scans SQL files for `[DatabaseName].[schema].[object]` patterns
2. Resolves referenced databases to other `.sqlproj` projects in the repo
3. Injects `ArtifactReference` elements so DACPACs compile without errors
4. Builds in dependency order (topological sort)

### Key Files

| File | Purpose |
|---|---|
| [`AzDO/Extract-Lakehouse-Schema.yml`](AzDO/Extract-Lakehouse-Schema.yml) | Pipeline to extract Lakehouse schemas from Fabric |
| [`AzDO/scripts/deploy/extract-lakehouse-schema.ps1`](AzDO/scripts/deploy/extract-lakehouse-schema.ps1) | PowerShell script for schema extraction |

---

## Cleanup

To remove all demo resources after the demo:

```powershell
# Delete Azure resources (Resource Group, Key Vault, SP)
az group delete --name rg-fabric-cicd-{postfix} --yes --no-wait
az ad sp delete --id <sp-client-id>

# Delete Fabric workspaces (via REST API or Fabric portal)
# Navigate to each workspace → Settings → Remove this workspace

# Delete Azure DevOps project
az devops project delete --id <project-id> --yes
```

---

## Troubleshooting

| Issue | Solution |
|---|---|
| Key Vault secrets "Forbidden" during setup | Grant your user `Key Vault Secrets Officer` role on the vault, or switch to access policies |
| Azure DevOps variable group can't link to Key Vault | Switch KV to access policy model (`--enable-rbac-authorization false`) and set `get, list` policy for the SP |
| Pipeline fails with "Workspace not found" | Check `mainWorkspaceName` / `testWorkspaceName` in `Fabric_Deployment_Group_NS` — must match workspace names exactly (case-sensitive) |
| Pipeline fails with "PrincipalNotFound" | Wait 2-3 minutes after SP creation for Fabric propagation, then re-add SP as workspace Admin |
| Pipeline fails with auth error | Verify the SP secret hasn't expired; reset with `az ad sp credential reset --id <client-id>` and update the KV secret |
| "Committing is not possible because you have unmerged files" | Merge conflicts in the repo — clone fresh, resolve conflicts, force push |
| Variable group doesn't show secrets | You need two separate groups — KV-linked groups can only contain vault secrets |
