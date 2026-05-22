<#
.SYNOPSIS
    Automated setup script for Fabric CI/CD Option 2 demo.
    Creates all Azure, Fabric, and Azure DevOps resources from scratch.

.DESCRIPTION
    This script automates the following:
    - Phase 1: Azure Resource Group, Service Principal, Key Vault with secrets
    - Phase 2: Fabric workspaces, capacity assignment, SP role assignment
    - Phase 3: Azure DevOps project, repo, branches, pipeline environments, pipeline creation

    Manual steps are clearly documented and paused for at the appropriate points.

.NOTES
    Prerequisites:
    - Azure CLI installed and logged in (az login)
    - Azure DevOps CLI extension (az extension add --name azure-devops)
    - Git CLI installed
    - A Fabric capacity (F or P SKU) already provisioned
    - An Azure DevOps organization already created

.PARAMETER SubscriptionId
    Azure subscription ID to use.
.PARAMETER Location
    Azure region for resource group and Key Vault.
.PARAMETER AzDoOrg
    Full URL of your Azure DevOps organization (e.g., https://dev.azure.com/myorg).
.PARAMETER AzDoPat
    Azure DevOps Personal Access Token with Full access scope.
.PARAMETER ResourcePostfix
    Optional postfix appended to all resource names for disambiguation.
    If not provided, a random 4-digit number is generated.
    Examples: "team1", "demo42", "westeu"
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$SubscriptionId,

    [Parameter(Mandatory = $false)]
    [string]$Location = "eastus",

    [Parameter(Mandatory = $true)]
    [string]$AzDoOrg,

    [Parameter(Mandatory = $true)]
    [string]$AzDoPat,

    [Parameter(Mandatory = $false)]
    [string]$ResourcePostfix
)

# ============================================================================
# Configuration — resource names use postfix for disambiguation
# ============================================================================
if (-not $ResourcePostfix) {
    $ResourcePostfix = "{0:D4}" -f (Get-Random -Maximum 9999)
    Write-Host "No postfix provided — generated random postfix: $ResourcePostfix" -ForegroundColor Yellow
}

$RESOURCE_GROUP    = "rg-fabric-cicd-$ResourcePostfix"
$KV_NAME           = "kv-fab-cicd-$ResourcePostfix"   # globally unique, max 24 chars
$SP_NAME           = "sp-fabric-cicd-$ResourcePostfix"
$AZDO_PROJECT      = "FabricCICD-$ResourcePostfix"
$AZDO_REPO         = "fabric-demo-repo"
$DEV_WORKSPACE     = "Demo-Dev-$ResourcePostfix"
$TEST_WORKSPACE    = "Demo-Test-$ResourcePostfix"
$SCRIPT_DIR        = $PSScriptRoot

# ============================================================================
# Helper
# ============================================================================
function Write-Phase {
    param([string]$Phase, [string]$Description)
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Cyan
    Write-Host " $Phase - $Description" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
}

function Write-Manual {
    param([string]$Message)
    Write-Host ""
    Write-Host "[MANUAL STEP REQUIRED]" -ForegroundColor Yellow
    Write-Host $Message -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Press ENTER when done to continue..."
}

function Write-Info {
    param([string]$Message)
    Write-Host "  -> $Message" -ForegroundColor Green
}

# ============================================================================
# PHASE 1: Azure & Entra ID Setup
# ============================================================================
Write-Phase "Phase 1" "Azure & Entra ID Setup"

# Set subscription
az account set --subscription $SubscriptionId
Write-Info "Subscription set to $SubscriptionId"

# Step 1.1 — Create Resource Group
Write-Info "Creating Resource Group: $RESOURCE_GROUP"
az group create --name $RESOURCE_GROUP --location $Location --output none

# Step 1.2 — Create Service Principal
Write-Info "Creating Service Principal: $SP_NAME"
$spJson = az ad sp create-for-rbac --name $SP_NAME --skip-assignment 2>$null
$sp = $spJson | ConvertFrom-Json
$TENANT_ID     = $sp.tenant
$CLIENT_ID     = $sp.appId
$CLIENT_SECRET = $sp.password

Write-Info "Tenant ID:     $TENANT_ID"
Write-Info "Client ID:     $CLIENT_ID"
Write-Info "Client Secret: ********** (stored in memory)"

# Step 1.3 — Create Key Vault
Write-Info "Creating Key Vault: $KV_NAME"
az keyvault create --name $KV_NAME --resource-group $RESOURCE_GROUP --location $Location --output none

# Step 1.3b — Grant current user 'Key Vault Secrets Officer' so we can write secrets
$currentUserOid = az ad signed-in-user show --query id -o tsv
Write-Info "Granting current user ($currentUserOid) 'Key Vault Secrets Officer' role"
az role assignment create `
    --role "Key Vault Secrets Officer" `
    --assignee-object-id $currentUserOid `
    --assignee-principal-type User `
    --scope "/subscriptions/$SubscriptionId/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.KeyVault/vaults/$KV_NAME" `
    --output none

Write-Info "Waiting 30 seconds for RBAC role assignment to propagate..."
Start-Sleep -Seconds 30

# Step 1.4 — Store secrets in Key Vault
Write-Info "Storing secrets in Key Vault"
az keyvault secret set --vault-name $KV_NAME --name "aztenantid" --value $TENANT_ID --output none
az keyvault secret set --vault-name $KV_NAME --name "azclientid" --value $CLIENT_ID --output none
az keyvault secret set --vault-name $KV_NAME --name "azspsecret" --value $CLIENT_SECRET --output none

# Step 1.5 — Grant SP access to Key Vault
Write-Info "Granting SP 'Key Vault Secrets User' role"
az role assignment create `
    --role "Key Vault Secrets User" `
    --assignee $CLIENT_ID `
    --scope "/subscriptions/$SubscriptionId/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.KeyVault/vaults/$KV_NAME" `
    --output none

Write-Host ""
Write-Host "Phase 1 Complete!" -ForegroundColor Green
Write-Host "  Resource Group: $RESOURCE_GROUP"
Write-Host "  Key Vault:      $KV_NAME"
Write-Host "  SP App ID:      $CLIENT_ID"

# ============================================================================
# PHASE 2: Fabric Workspace Setup
# ============================================================================
Write-Phase "Phase 2" "Fabric Workspace Setup"

# Get Fabric API token (uses the logged-in user's identity)
Write-Info "Acquiring Fabric API token"
$fabricToken = az account get-access-token --resource https://api.fabric.microsoft.com --query accessToken -o tsv
$headers = @{
    "Authorization" = "Bearer $fabricToken"
    "Content-Type"  = "application/json"
}

# Step 2.1 — Create Dev workspace
Write-Info "Creating workspace: $DEV_WORKSPACE"
$devBody = @{ displayName = $DEV_WORKSPACE } | ConvertTo-Json
try {
    $devWs = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces" -Method POST -Headers $headers -Body $devBody
    $DEV_WS_ID = $devWs.id
    Write-Info "Dev Workspace ID: $DEV_WS_ID"
} catch {
    $err = $_.ErrorDetails.Message | ConvertFrom-Json -ErrorAction SilentlyContinue
    if ($err.errorCode -eq "WorkspaceNameAlreadyExists") {
        Write-Host "  Workspace '$DEV_WORKSPACE' already exists — looking up its ID" -ForegroundColor Yellow
        $allWs = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces" -Method GET -Headers $headers
        $existing = $allWs.value | Where-Object { $_.displayName -eq $DEV_WORKSPACE }
        $DEV_WS_ID = $existing.id
        Write-Info "Dev Workspace ID (existing): $DEV_WS_ID"
    } else { throw }
}

# Step 2.2 — Create Test workspace
Write-Info "Creating workspace: $TEST_WORKSPACE"
$testBody = @{ displayName = $TEST_WORKSPACE } | ConvertTo-Json
try {
    $testWs = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces" -Method POST -Headers $headers -Body $testBody
    $TEST_WS_ID = $testWs.id
    Write-Info "Test Workspace ID: $TEST_WS_ID"
} catch {
    $err = $_.ErrorDetails.Message | ConvertFrom-Json -ErrorAction SilentlyContinue
    if ($err.errorCode -eq "WorkspaceNameAlreadyExists") {
        Write-Host "  Workspace '$TEST_WORKSPACE' already exists — looking up its ID" -ForegroundColor Yellow
        if (-not $allWs) { $allWs = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces" -Method GET -Headers $headers }
        $existing = $allWs.value | Where-Object { $_.displayName -eq $TEST_WORKSPACE }
        $TEST_WS_ID = $existing.id
        Write-Info "Test Workspace ID (existing): $TEST_WS_ID"
    } else { throw }
}

# Step 2.3 — List capacities and assign
Write-Info "Listing available Fabric capacities"
$capacities = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/capacities" -Method GET -Headers $headers
$capacities.value | ForEach-Object { Write-Host "    $($_.displayName)  ->  $($_.id)" }

if ($capacities.value.Count -eq 1) {
    $CAPACITY_ID = $capacities.value[0].id
    Write-Info "Auto-selected capacity: $CAPACITY_ID"
} else {
    Write-Host ""
    $CAPACITY_ID = Read-Host "Enter the Capacity ID to use"
}

Write-Info "Assigning $DEV_WORKSPACE to capacity"
$capBody = @{ capacityId = $CAPACITY_ID } | ConvertTo-Json
Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$DEV_WS_ID/assignToCapacity" -Method POST -Headers $headers -Body $capBody

Write-Info "Assigning $TEST_WORKSPACE to capacity"
Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$TEST_WS_ID/assignToCapacity" -Method POST -Headers $headers -Body $capBody

# Step 2.4 — Add SP as Admin on both workspaces
$SP_OBJECT_ID = az ad sp show --id $CLIENT_ID --query id -o tsv
Write-Info "Adding SP as Admin on both workspaces (SP Object ID: $SP_OBJECT_ID)"
$spRoleBody = @{
    principal = @{
        id   = $SP_OBJECT_ID
        type = "ServicePrincipal"
    }
    role = "Admin"
} | ConvertTo-Json -Depth 3

try {
    Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$DEV_WS_ID/roleAssignments" -Method POST -Headers $headers -Body $spRoleBody
} catch {
    $err = $_.ErrorDetails.Message | ConvertFrom-Json -ErrorAction SilentlyContinue
    if ($err.errorCode -eq "PrincipalAlreadyHasWorkspaceRolePermissions") {
        Write-Host "  SP already has a role on '$DEV_WORKSPACE' — skipping" -ForegroundColor Yellow
    } else { throw }
}

try {
    Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$TEST_WS_ID/roleAssignments" -Method POST -Headers $headers -Body $spRoleBody
} catch {
    $err = $_.ErrorDetails.Message | ConvertFrom-Json -ErrorAction SilentlyContinue
    if ($err.errorCode -eq "PrincipalAlreadyHasWorkspaceRolePermissions") {
        Write-Host "  SP already has a role on '$TEST_WORKSPACE' — skipping" -ForegroundColor Yellow
    } else { throw }
}

# --- Step 2.5: Seed demo content via Fabric REST API ---
Write-Info "Seeding demo content in '$DEV_WORKSPACE'"

# Helper: Create a Fabric item, handle "already exists" gracefully
function New-FabricItem {
    param([string]$WorkspaceId, [hashtable]$Headers, [string]$DisplayName, [string]$Type, [hashtable]$Definition)
    $body = @{ displayName = $DisplayName; type = $Type }
    if ($Definition) { $body.definition = $Definition }
    $json = $body | ConvertTo-Json -Depth 10
    try {
        $item = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$WorkspaceId/items" `
            -Method POST -Headers $Headers -Body $json
        Write-Info "Created $Type '$DisplayName' (ID: $($item.id))"
        return $item
    } catch {
        $err = $_.ErrorDetails.Message | ConvertFrom-Json -ErrorAction SilentlyContinue
        if ($err.errorCode -eq "ItemDisplayNameAlreadyInUse") {
            Write-Host "  $Type '$DisplayName' already exists — skipping" -ForegroundColor Yellow
            $items = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$WorkspaceId/items?type=$Type" `
                -Method GET -Headers $Headers
            return $items.value | Where-Object { $_.displayName -eq $DisplayName }
        } else { throw }
    }
}

# 2.5a — Create Lakehouse
$lakehouse = New-FabricItem -WorkspaceId $DEV_WS_ID -Headers $headers `
    -DisplayName "DemoLakehouse" -Type "Lakehouse"

# 2.5b — Create Notebook (with inline definition)
$notebookContent = @{
    nbformat       = 4
    nbformat_minor = 5
    metadata       = @{
        language_info       = @{ name = "python" }
        trident             = @{ lakehouse = @{ known_lakehouses = @(
            @{ id = $lakehouse.id }
        ) } }
    }
    cells = @(
        @{
            cell_type = "code"
            source    = @('print("Hello from Dev!")')
            metadata  = @{}
            outputs   = @()
        }
    )
} | ConvertTo-Json -Depth 10 -Compress

$notebookPayload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($notebookContent))
$notebookDef = @{
    format = "ipynb"
    parts  = @(
        @{ path = "notebook-content.py"; payload = $notebookPayload; payloadType = "InlineBase64" }
    )
}
$notebook = New-FabricItem -WorkspaceId $DEV_WS_ID -Headers $headers `
    -DisplayName "HelloWorld" -Type "Notebook" -Definition $notebookDef

# 2.5c — Create Warehouse
$warehouse = New-FabricItem -WorkspaceId $DEV_WS_ID -Headers $headers `
    -DisplayName "DemoWarehouse" -Type "Warehouse"

# 2.5d — Create table in Warehouse via SQL
if ($warehouse.id) {
    Write-Info "Waiting 60 seconds for Warehouse provisioning to complete..."
    Start-Sleep -Seconds 60

    # Get warehouse connection info
    $whProps = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$DEV_WS_ID/warehouses/$($warehouse.id)" `
        -Method GET -Headers $headers
    $whConnStr = $whProps.properties.connectionString

    if ($whConnStr) {
        Write-Info "Running CREATE TABLE on $whConnStr"
        $whToken = az account get-access-token --resource https://database.windows.net --query accessToken -o tsv
        try {
            Invoke-Sqlcmd -ServerInstance $whConnStr -Database $warehouse.displayName `
                -AccessToken $whToken `
                -Query "IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name='DemoTable') CREATE TABLE dbo.DemoTable (Id INT, Name VARCHAR(100))" `
                -TrustServerCertificate
            Write-Info "Table dbo.DemoTable created"
        } catch {
            Write-Host "  WARNING: Could not run SQL — create the table manually:" -ForegroundColor Yellow
            Write-Host "    Server: $whConnStr" -ForegroundColor Yellow
            Write-Host "    SQL: CREATE TABLE dbo.DemoTable (Id INT, Name VARCHAR(100))" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  WARNING: Warehouse connection string not available yet" -ForegroundColor Yellow
        Write-Host "    Create the table manually after provisioning completes" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Phase 2 Complete!" -ForegroundColor Green
Write-Host "  Dev Workspace:  $DEV_WORKSPACE ($DEV_WS_ID)"
Write-Host "  Test Workspace: $TEST_WORKSPACE ($TEST_WS_ID)"
Write-Host "  Items created:  Lakehouse, Notebook, Warehouse"

# ============================================================================
# PHASE 3: Azure DevOps Setup
# ============================================================================
Write-Phase "Phase 3" "Azure DevOps Setup"

# Configure Azure DevOps defaults
Write-Info "Configuring Azure DevOps CLI"
$env:AZURE_DEVOPS_EXT_PAT = $AzDoPat
az devops configure --defaults organization=$AzDoOrg

# Step 3.1 — Create project
Write-Info "Creating Azure DevOps project: $AZDO_PROJECT"
az devops project create --name $AZDO_PROJECT --visibility private --output none 2>$null
az devops configure --defaults project=$AZDO_PROJECT

# Step 3.2 — Create repo
Write-Info "Creating repository: $AZDO_REPO"
$repoJson = az repos create --name $AZDO_REPO --output json 2>$null
$repo = $repoJson | ConvertFrom-Json

# Initialize repo with a README so main branch exists
Write-Info "Initializing repository with README"
az repos import create `
    --git-source-url "https://github.com/microsoft/fabric-cicd.git" `
    --repository $AZDO_REPO `
    --output none 2>$null

# If import doesn't work, we'll push manually later. Let's just try a push approach.
# Clone, add files, push.

$CLONE_DIR = Join-Path $env:TEMP "fabric-demo-repo-$(Get-Random)"
Write-Info "Cloning repo to $CLONE_DIR"

# Construct authenticated clone URL
$orgName = ($AzDoOrg -replace 'https://dev.azure.com/', '')
$cloneUrl = "https://$($AzDoPat)@dev.azure.com/$orgName/$AZDO_PROJECT/_git/$AZDO_REPO"

git clone $cloneUrl $CLONE_DIR 2>$null

# If repo is empty, initialize it
Push-Location $CLONE_DIR
$isEmptyRepo = -not (Test-Path ".git/HEAD" -PathType Leaf) -or ((git log --oneline 2>$null) -eq $null)

if ($isEmptyRepo -or -not (Test-Path "README.md")) {
    Write-Info "Initializing empty repo with README"
    "# Fabric CI/CD Demo" | Out-File -FilePath "README.md" -Encoding utf8
    git add README.md
    git commit -m "Initial commit" 2>$null
    git push origin main 2>$null
    # If main doesn't exist as default, try master
    git branch -M main 2>$null
    git push -u origin main 2>$null
}
Pop-Location

# Step 3.3 — Create ARM Service Connection
Write-Info "Creating ARM service connection: azure-kv-connection"
$azDoHeaders = @{
    "Authorization" = "Basic $([Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes(":$AzDoPat")))"
    "Content-Type"  = "application/json"
}

# Get the project ID
$projectInfo = az devops project show --project $AZDO_PROJECT --output json | ConvertFrom-Json
$projectId = $projectInfo.id

$serviceConnectionBody = @{
    name = "azure-kv-connection"
    type = "azurerm"
    url  = "https://management.azure.com/"
    authorization = @{
        parameters = @{
            tenantid            = $TENANT_ID
            serviceprincipalid  = $CLIENT_ID
            authenticationType  = "spnKey"
            serviceprincipalkey = $CLIENT_SECRET
        }
        scheme = "ServicePrincipal"
    }
    data = @{
        subscriptionId   = $SubscriptionId
        subscriptionName = (az account show --query name -o tsv)
        environment      = "AzureCloud"
        scopeLevel       = "Subscription"
    }
    serviceEndpointProjectReferences = @(
        @{
            projectReference = @{ id = $projectId; name = $AZDO_PROJECT }
            name             = "azure-kv-connection"
        }
    )
} | ConvertTo-Json -Depth 5

try {
    $scResponse = Invoke-RestMethod `
        -Uri "$AzDoOrg/_apis/serviceendpoint/endpoints?api-version=7.1" `
        -Method POST -Headers $azDoHeaders -Body $serviceConnectionBody
    $SERVICE_CONNECTION_ID = $scResponse.id
    Write-Info "Service connection created (ID: $SERVICE_CONNECTION_ID)"

    # Grant pipeline access to the service connection
    $scGrantBody = @{
        pipelines = @()
        resource  = @{ id = $SERVICE_CONNECTION_ID; type = "endpoint" }
        allPipelines = @{ authorized = $true }
    } | ConvertTo-Json -Depth 5
    Invoke-RestMethod `
        -Uri "$AzDoOrg/$AZDO_PROJECT/_apis/pipelines/pipelinepermissions/endpoint/$SERVICE_CONNECTION_ID`?api-version=7.1-preview.1" `
        -Method PATCH -Headers $azDoHeaders -Body $scGrantBody | Out-Null
    Write-Info "Service connection authorized for all pipelines"
} catch {
    $err = $_.ErrorDetails.Message
    if ($err -match "already exists") {
        Write-Host "  Service connection 'azure-kv-connection' already exists — looking up ID" -ForegroundColor Yellow
        $existingScs = Invoke-RestMethod -Uri "$AzDoOrg/$AZDO_PROJECT/_apis/serviceendpoint/endpoints?endpointNames=azure-kv-connection&api-version=7.1" `
            -Method GET -Headers $azDoHeaders
        $SERVICE_CONNECTION_ID = $existingScs.value[0].id
        Write-Info "Existing service connection ID: $SERVICE_CONNECTION_ID"
    } else {
        Write-Host "  WARNING: Could not create service connection automatically" -ForegroundColor Yellow
        Write-Host "  Error: $err" -ForegroundColor Yellow
        Write-Manual @"
Create an ARM Service Connection manually in Azure DevOps:
  1. Go to: $AzDoOrg/$AZDO_PROJECT/_settings/adminservices
  2. Click 'New service connection' -> 'Azure Resource Manager' -> 'Service principal (manual)'
  3. Subscription ID: $SubscriptionId
  4. Service Principal ID: $CLIENT_ID
  5. Service Principal Key: (use the SP secret)
  6. Tenant ID: $TENANT_ID
  7. Scope: resource group '$RESOURCE_GROUP'
  8. Name it: 'azure-kv-connection'
"@
    }
}

# Step 3.4 — Git sync from Fabric via REST API
Write-Info "Connecting '$DEV_WORKSPACE' to Git and committing item definitions"

# Refresh Fabric token (may have expired during earlier steps)
$fabricToken = az account get-access-token --resource https://api.fabric.microsoft.com --query accessToken -o tsv
$headers = @{
    "Authorization" = "Bearer $fabricToken"
    "Content-Type"  = "application/json"
}

# 3.4a — Connect workspace to Git
$connectBody = @{
    gitProviderDetails = @{
        gitProviderType  = "AzureDevOps"
        organizationName = $orgName
        projectName      = $AZDO_PROJECT
        repositoryName   = $AZDO_REPO
        branchName       = "main"
        directoryName    = "/fabric"
    }
} | ConvertTo-Json -Depth 5

try {
    Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$DEV_WS_ID/git/connect" `
        -Method POST -Headers $headers -Body $connectBody
    Write-Info "Workspace connected to Git"
} catch {
    $err = $_.ErrorDetails.Message | ConvertFrom-Json -ErrorAction SilentlyContinue
    Write-Host "  WARNING: Git connect failed — $($err.message)" -ForegroundColor Yellow
    Write-Host "  You may need to connect manually via the Fabric portal" -ForegroundColor Yellow
}

# 3.4b — Initialize connection
Write-Info "Initializing Git connection"
$initResponse = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$DEV_WS_ID/git/initializeConnection" `
    -Method POST -Headers $headers -Body "{}"

# 3.4c — Commit all workspace items to Git
Write-Info "Committing workspace items to Git"
$commitBody = @{
    mode          = "All"
    comment       = "Initial commit — seed demo content from Fabric workspace"
} | ConvertTo-Json -Depth 3

$commitResponse = Invoke-WebRequest -Uri "https://api.fabric.microsoft.com/v1/workspaces/$DEV_WS_ID/git/commitToGit" `
    -Method POST -Headers $headers -Body $commitBody

# Poll LRO if returned
$operationId = $commitResponse.Headers['x-ms-operation-id']
if ($operationId) {
    Write-Info "Commit operation started (ID: $operationId) — polling for completion"
    $lroUrl = "https://api.fabric.microsoft.com/v1/operations/$operationId"
    $retryAfter = if ($commitResponse.Headers['Retry-After']) { [int]$commitResponse.Headers['Retry-After'] } else { 5 }
    do {
        Start-Sleep -Seconds $retryAfter
        $opState = Invoke-RestMethod -Uri $lroUrl -Method GET -Headers $headers
        Write-Host "    Commit status: $($opState.status)" -ForegroundColor Gray
    } while ($opState.status -in @("NotStarted", "Running"))
    if ($opState.status -eq "Succeeded") {
        Write-Info "All items committed to Git successfully"
    } else {
        Write-Host "  WARNING: Commit operation ended with status: $($opState.status)" -ForegroundColor Yellow
    }
} else {
    Write-Info "Commit completed"
}

# 3.4d — Disconnect workspace from Git
Write-Info "Disconnecting workspace from Git"
Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$DEV_WS_ID/git/disconnect" `
    -Method POST -Headers $headers
Write-Info "Workspace disconnected from Git"

# Pull latest to get the fabric/ folder
Push-Location $CLONE_DIR
Write-Info "Pulling latest after Fabric sync"
git pull origin main 2>$null

# Step 3.5 — Copy pipeline files into repo
Write-Info "Copying pipeline and deployment files to repo"
Copy-Item "$SCRIPT_DIR\AzDO\Deploy-To-Fabric.yml" -Destination "$CLONE_DIR\Deploy-To-Fabric.yml" -Force
New-Item -ItemType Directory -Path "$CLONE_DIR\scripts\deploy" -Force | Out-Null
Copy-Item "$SCRIPT_DIR\AzDO\scripts\deploy\deploy-to-fabric.py" -Destination "$CLONE_DIR\scripts\deploy\deploy-to-fabric.py" -Force
Copy-Item "$SCRIPT_DIR\AzDO\scripts\deploy\post_deployment.py" -Destination "$CLONE_DIR\scripts\deploy\post_deployment.py" -Force
if (Test-Path "$SCRIPT_DIR\AzDO\scripts\deploy\mapping_connections.template.json") {
    Copy-Item "$SCRIPT_DIR\AzDO\scripts\deploy\mapping_connections.template.json" -Destination "$CLONE_DIR\scripts\deploy\mapping_connections.template.json" -Force
}

# Commit and push pipeline files
git add Deploy-To-Fabric.yml scripts/deploy/
git commit -m "Add CI/CD pipeline, deployment, and post-deployment scripts"
git push origin main 2>$null
Pop-Location

# Step 3.6 — Create pipeline environments via REST API
Write-Info "Creating pipeline environments (dev, test)"
$base64Auth = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes(":$AzDoPat"))
$azDoHeaders = @{
    "Authorization" = "Basic $base64Auth"
    "Content-Type"  = "application/json"
}

try {
    Invoke-RestMethod -Uri "$AzDoOrg/$AZDO_PROJECT/_apis/pipelines/environments?api-version=7.1" `
        -Method POST -Headers $azDoHeaders `
        -Body (@{ name = "dev"; description = "Development environment" } | ConvertTo-Json) | Out-Null
    Write-Info "Environment 'dev' created"
} catch {
    Write-Host "  WARNING: Could not create 'dev' environment (may already exist)" -ForegroundColor Yellow
}

try {
    Invoke-RestMethod -Uri "$AzDoOrg/$AZDO_PROJECT/_apis/pipelines/environments?api-version=7.1" `
        -Method POST -Headers $azDoHeaders `
        -Body (@{ name = "test"; description = "Test environment" } | ConvertTo-Json) | Out-Null
    Write-Info "Environment 'test' created"
} catch {
    Write-Host "  WARNING: Could not create 'test' environment (may already exist)" -ForegroundColor Yellow
}

# Step 3.7 — Create Variable Groups
Write-Info "Creating variable group: Fabric_Deployment_Group_NS (plain variables)"
$nsGroupBody = @{
    name      = "Fabric_Deployment_Group_NS"
    type      = "Vsts"
    variables = @{
        mainWorkspaceName = @{ value = $DEV_WORKSPACE }
        testWorkspaceName = @{ value = $TEST_WORKSPACE }
        gitDirectory      = @{ value = "fabric" }
    }
    variableGroupProjectReferences = @(
        @{
            name             = "Fabric_Deployment_Group_NS"
            projectReference = @{ id = $projectId; name = $AZDO_PROJECT }
        }
    )
} | ConvertTo-Json -Depth 5

try {
    $nsGroup = Invoke-RestMethod -Uri "$AzDoOrg/$AZDO_PROJECT/_apis/distributedtask/variablegroups?api-version=7.1" `
        -Method POST -Headers $azDoHeaders -Body $nsGroupBody
    Write-Info "Variable group 'Fabric_Deployment_Group_NS' created (ID: $($nsGroup.id))"
} catch {
    $err = $_.ErrorDetails.Message
    if ($err -match "already exists") {
        Write-Host "  Variable group 'Fabric_Deployment_Group_NS' already exists — skipping" -ForegroundColor Yellow
    } else {
        Write-Host "  WARNING: Could not create variable group: $err" -ForegroundColor Yellow
    }
}

if ($SERVICE_CONNECTION_ID) {
    Write-Info "Creating variable group: Fabric_Deployment_Group_S (Key Vault linked)"
    $sGroupBody = @{
        name                          = "Fabric_Deployment_Group_S"
        type                          = "AzureKeyVault"
        providerData                  = @{
            serviceEndpointId = $SERVICE_CONNECTION_ID
            vault             = $KV_NAME
            lastRefreshedOn   = (Get-Date).ToUniversalTime().ToString("o")
        }
        variables = @{
            aztenantid = @{ enabled = $true; contentType = ""; value = ""; isSecret = $true }
            azclientid = @{ enabled = $true; contentType = ""; value = ""; isSecret = $true }
            azspsecret = @{ enabled = $true; contentType = ""; value = ""; isSecret = $true }
        }
        variableGroupProjectReferences = @(
            @{
                name             = "Fabric_Deployment_Group_S"
                projectReference = @{ id = $projectId; name = $AZDO_PROJECT }
            }
        )
    } | ConvertTo-Json -Depth 5

    try {
        $sGroup = Invoke-RestMethod -Uri "$AzDoOrg/$AZDO_PROJECT/_apis/distributedtask/variablegroups?api-version=7.1" `
            -Method POST -Headers $azDoHeaders -Body $sGroupBody
        Write-Info "Variable group 'Fabric_Deployment_Group_S' created (ID: $($sGroup.id))"
    } catch {
        $err = $_.ErrorDetails.Message
        if ($err -match "already exists") {
            Write-Host "  Variable group 'Fabric_Deployment_Group_S' already exists — skipping" -ForegroundColor Yellow
        } else {
            Write-Host "  WARNING: Could not create KV-linked variable group: $err" -ForegroundColor Yellow
            Write-Manual @"
Create 'Fabric_Deployment_Group_S' manually (Key Vault linked):
  1. Go to: $AzDoOrg/$AZDO_PROJECT/_library
  2. Click '+ Variable group' -> Name: Fabric_Deployment_Group_S
  3. Toggle ON 'Link secrets from an Azure key vault as variables'
  4. Service connection: azure-kv-connection
  5. Key vault name: $KV_NAME
  6. Click 'Add' and authorize: aztenantid, azclientid, azspsecret -> Save
"@
        }
    }
} else {
    Write-Manual @"
Create two Variable Groups manually in Azure DevOps:
  1. Go to: $AzDoOrg/$AZDO_PROJECT/_library
  2. Create 'Fabric_Deployment_Group_S' (Key Vault linked) with secrets: aztenantid, azclientid, azspsecret
  3. Create 'Fabric_Deployment_Group_NS' (plain) with: mainWorkspaceName=$DEV_WORKSPACE, testWorkspaceName=$TEST_WORKSPACE, gitDirectory=fabric
"@
}

# Step 3.8 — Create the pipeline
Write-Info "Creating pipeline: Deploy-To-Fabric"
$PIPELINE_ID = $null
try {
    $pipelineJson = az pipelines create `
        --name "Deploy-To-Fabric" `
        --repository $AZDO_REPO `
        --repository-type tfsgit `
        --branch main `
        --yml-path Deploy-To-Fabric.yml `
        --skip-first-run `
        --output json 2>$null
    $pipelineObj = $pipelineJson | ConvertFrom-Json
    $PIPELINE_ID = $pipelineObj.id
    Write-Info "Pipeline 'Deploy-To-Fabric' created (ID: $PIPELINE_ID)"
} catch {
    Write-Host "  WARNING: Pipeline creation failed — looking up existing pipeline" -ForegroundColor Yellow
}

if (-not $PIPELINE_ID) {
    try {
        $pipelines = az pipelines list --output json 2>$null | ConvertFrom-Json
        $existing = $pipelines | Where-Object { $_.name -eq "Deploy-To-Fabric" }
        if ($existing) {
            $PIPELINE_ID = $existing.id
            Write-Info "Found existing pipeline ID: $PIPELINE_ID"
        }
    } catch { }
}

# Step 3.9 — Add approval gate on 'test' environment
Write-Info "Looking up 'test' environment ID"
$TEST_ENV_ID = $null
try {
    $envs = Invoke-RestMethod -Uri "$AzDoOrg/$AZDO_PROJECT/_apis/pipelines/environments?api-version=7.1" `
        -Method GET -Headers $azDoHeaders
    $testEnv = $envs.value | Where-Object { $_.name -eq "test" }
    $TEST_ENV_ID = $testEnv.id
    Write-Info "Environment 'test' ID: $TEST_ENV_ID"
} catch {
    Write-Host "  WARNING: Could not look up 'test' environment" -ForegroundColor Yellow
}

if ($TEST_ENV_ID) {
    Write-Info "Adding approval check on 'test' environment"
    # Get current user identity from AzDO
    $currentUser = Invoke-RestMethod -Uri "$AzDoOrg/_apis/connectionData?api-version=7.1-preview" `
        -Method GET -Headers $azDoHeaders
    $userId = $currentUser.authenticatedUser.id
    $userDisplayName = $currentUser.authenticatedUser.providerDisplayName

    $approvalBody = @{
        type     = @{
            id   = "8c6f20a7-a545-4486-9777-f762fafe0d4d"  # Approval type GUID
            name = "Approval"
        }
        settings = @{
            approvers           = @(
                @{
                    id = $userId
                    displayName = $userDisplayName
                }
            )
            executionOrder      = "anyOrder"
            minRequiredApprovers = 1
            instructions        = "Approve deployment to Test workspace"
        }
        resource = @{
            type = "environment"
            id   = "$TEST_ENV_ID"
        }
    } | ConvertTo-Json -Depth 5

    try {
        Invoke-RestMethod -Uri "$AzDoOrg/$AZDO_PROJECT/_apis/pipelines/checks/configurations?api-version=7.2-preview.1" `
            -Method POST -Headers $azDoHeaders -Body $approvalBody | Out-Null
        Write-Info "Approval gate added to 'test' environment (approver: $userDisplayName)"
    } catch {
        $err = $_.ErrorDetails.Message
        Write-Host "  WARNING: Could not add approval gate: $err" -ForegroundColor Yellow
        Write-Host "  Add manually: $AzDoOrg/$AZDO_PROJECT/_environments -> test -> Approvals and checks" -ForegroundColor Yellow
    }
} else {
    Write-Host "  WARNING: Skipping approval gate — could not find 'test' environment" -ForegroundColor Yellow
}

# Step 3.10 — Grant pipeline access to variable groups
if ($PIPELINE_ID) {
    # Grant access to NS group
    $allVarGroups = Invoke-RestMethod -Uri "$AzDoOrg/$AZDO_PROJECT/_apis/distributedtask/variablegroups?api-version=7.1" `
        -Method GET -Headers $azDoHeaders
    foreach ($vgName in @("Fabric_Deployment_Group_NS", "Fabric_Deployment_Group_S")) {
        $vg = $allVarGroups.value | Where-Object { $_.name -eq $vgName }
        if ($vg) {
            Write-Info "Granting pipeline access to variable group: $vgName"
            $vgGrantBody = @{
                pipelines = @(
                    @{
                        id         = $PIPELINE_ID
                        authorized = $true
                    }
                )
                resource = @{ id = "$($vg.id)"; type = "variablegroup" }
            } | ConvertTo-Json -Depth 5

            try {
                Invoke-RestMethod `
                    -Uri "$AzDoOrg/$AZDO_PROJECT/_apis/pipelines/pipelinepermissions/variablegroup/$($vg.id)?api-version=7.1-preview.1" `
                    -Method PATCH -Headers $azDoHeaders -Body $vgGrantBody | Out-Null
                Write-Info "Pipeline authorized for '$vgName'"
            } catch {
                Write-Host "  WARNING: Could not authorize pipeline for '$vgName'" -ForegroundColor Yellow
            }
        } else {
            Write-Host "  WARNING: Variable group '$vgName' not found — skipping permission grant" -ForegroundColor Yellow
        }
    }
} else {
    Write-Manual @"
Grant pipeline access to variable groups:
  - Go to: $AzDoOrg/$AZDO_PROJECT/_library
  - Click 'Fabric_Deployment_Group_S' -> 'Pipeline permissions' -> '+' -> select 'Deploy-To-Fabric'
  - Click 'Fabric_Deployment_Group_NS' -> 'Pipeline permissions' -> '+' -> select 'Deploy-To-Fabric'
"@
}

# ============================================================================
# SUMMARY
# ============================================================================
Write-Phase "Setup Complete" "Summary"

Write-Host ""
Write-Host "Azure Resources:" -ForegroundColor White
Write-Host "  Resource Group:    $RESOURCE_GROUP"
Write-Host "  Key Vault:         $KV_NAME"
Write-Host "  Service Principal: $SP_NAME (App ID: $CLIENT_ID)"
Write-Host ""
Write-Host "Fabric Workspaces:" -ForegroundColor White
Write-Host "  Dev:  $DEV_WORKSPACE ($DEV_WS_ID)"
Write-Host "  Test: $TEST_WORKSPACE ($TEST_WS_ID)"
Write-Host "  Capacity: $CAPACITY_ID"
Write-Host ""
Write-Host "Azure DevOps:" -ForegroundColor White
Write-Host "  Project:    $AZDO_PROJECT"
Write-Host "  Repo:       $AZDO_REPO"
Write-Host "  Pipeline:   Deploy-To-Fabric"
Write-Host "  Clone dir:  $CLONE_DIR"
Write-Host ""
Write-Host "Item types in scope:" -ForegroundColor White
Write-Host '  Notebook, DataPipeline, Lakehouse, SemanticModel, Report, Warehouse'
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Run a VALIDATION pipeline test: $AzDoOrg/$AZDO_PROJECT/_build (manually trigger on main)"
Write-Host "  2. Proceed to the live demo (see demo-script.ps1)"
Write-Host ""

# ============================================================================
# OUTPUT CONFIG FILE — used as input for demo-script.ps1
# ============================================================================
$configFile = Join-Path $SCRIPT_DIR "demo-config.json"
$config = [ordered]@{
    RepoPath        = $CLONE_DIR
    AzDoOrg         = $AzDoOrg
    AzDoProject     = $AZDO_PROJECT
    AzDoRepo        = $AZDO_REPO
    ResourceGroup   = $RESOURCE_GROUP
    KeyVaultName    = $KV_NAME
    SpName          = $SP_NAME
    SpClientId      = $CLIENT_ID
    TenantId        = $TENANT_ID
    DevWorkspace    = $DEV_WORKSPACE
    DevWorkspaceId  = $DEV_WS_ID
    TestWorkspace   = $TEST_WORKSPACE
    TestWorkspaceId = $TEST_WS_ID
    CapacityId      = $CAPACITY_ID
    ResourcePostfix = $ResourcePostfix
}
$config | ConvertTo-Json | Out-File -FilePath $configFile -Encoding utf8
Write-Host "Config file written to: $configFile" -ForegroundColor Green
Write-Host "  Use with demo-script.ps1:  .\demo-script.ps1 -ConfigFile `"$configFile`"" -ForegroundColor Green
Write-Host ""

# Cleanup temp vars
Remove-Item Env:\AZURE_DEVOPS_EXT_PAT -ErrorAction SilentlyContinue
