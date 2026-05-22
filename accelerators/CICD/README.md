# Microsoft Fabric CI/CD Accelerators

This folder contains a collection of accelerators that demonstrate different CI/CD strategies for Microsoft Fabric. Each subfolder provides end-to-end scripts, pipeline definitions, and documentation for a specific deployment approach.

## Accelerators

### [automate-wh-sqlendpoint-deployment](automate-wh-sqlendpoint-deployment/)

A **.NET application** for automating the deployment and management of Microsoft Fabric Data Warehouses and SQL Analytics Endpoints (Lakehouses). It performs intelligent dependency analysis across warehouses and SQL endpoints, extracts DACPACs, generates SQL projects, resolves cross-warehouse references using SQLCMD variables, and publishes schemas to target environments — all orchestrated through the Fabric REST APIs.

**Key capabilities:** workspace scanning, dependency chain resolution, DACPAC extraction, cross-database reference management, SQL project generation, and automated deployment with SQL endpoint metadata refresh.

### [Branch-out-to-new-workspace](Branch-out-to-new-workspace/)

Scripts and Azure DevOps pipeline definitions to **programmatically branch out to a new Fabric workspace** for isolated feature development. This is useful when developers cannot use the built-in UI capability due to permission constraints. The process is split into two steps — creating the workspace/branch via a release pipeline, and running post-activity tasks via a Fabric Notebook to rebind connections and configure item-level dependencies.

**Key capabilities:** automated workspace creation, DevOps branch creation, post-deployment connection rebinding, support for multiple authentication methods (access tokens, service accounts, service principals, PATs).

### [Deploy-using-Fabric-deployment-pipelines](Deploy-using-Fabric-deployment-pipelines/)

Azure DevOps YAML pipelines and PowerShell/Python scripts that use **Fabric Deployment Pipelines** to promote changes between environments (Dev → Test → Prod). Git is connected only to the Dev stage; subsequent stage deployments use the Fabric Deployment Pipelines REST APIs. Includes post-deployment notebook execution for connection rebinding and other configuration tasks.

**Key capabilities:** Git-to-Dev sync via Fabric Git APIs, stage-to-stage deployment via Deployment Pipelines APIs, post-deployment activity automation, Azure DevOps variable group configuration.

### [Git-based-deployment-with-sqlproj-schemas](Git-based-deployment-with-sqlproj-schemas/)

An accelerator for **automated CI/CD of SQL schemas** in Fabric Lakehouses and Warehouses. It extracts SQL schemas from Fabric SQL endpoints into version-controlled `.sqlproj` files, auto-detects cross-database dependencies, builds DACPACs in topological order, and deploys schemas to multiple environments using SqlPackage. Schema drift is tracked through Git with automated branch creation.

**Key capabilities:** SQL schema extraction, automatic dependency detection from SQL code, cross-database reference resolution, topological build ordering, branch-based multi-environment promotion (dev → test → prod).

### [Git-based-deployments](Git-based-deployments/)

An implementation of **Option 1** from the official Fabric CI/CD documentation — all deployments originate from the Git repository. Each release stage (Dev, Test, Prod) has a dedicated primary branch that feeds its corresponding Fabric workspace using Fabric Git APIs. Includes Azure DevOps pipelines, pre/post-deployment notebooks, connection mapping, and OneLake role/rule management.

**Key capabilities:** Git as single source of truth, Gitflow branching strategy support, workspace updates via Fabric Git APIs, connection remapping across stages, OneLake security role management.

### [Git-based-deployments-using-Build-environments](Git-based-deployments-using-Build-environments/)

An implementation of **Option 2** from the official Fabric CI/CD documentation, using the [`fabric-cicd`](https://pypi.org/project/fabric-cicd/) Python library as a code-first deployment solution. Dev and higher environments are non-Git-integrated; all deployments originate from Git branches via Azure DevOps pipelines. Feature development uses Git-integrated workspaces on feature branches, with changes merged via approved PRs that trigger automated deployments.

**Key capabilities:** code-first deployments using `fabric-cicd` library, service principal authentication, branch policy enforcement, unit testing in dev before promotion, non-Git-integrated target environments.
