// ===========================================================================
// Linux Consumption Function App (Python 3.11), system-assigned identity.
// Configured for identity-based AzureWebJobsStorage so no connection strings
// are stored. Blob trigger reads from the same storage account.
// ===========================================================================

@description('Azure region.')
param location string

@description('Tags applied to all resources.')
param tags object = {}

param functionAppName    string
param hostingPlanName    string
param storageAccountName string

@description('App Insights connection string.')
param appInsightsConnString string

@description('FQDN of the Azure SQL server, e.g. myserver.database.windows.net')
param sqlServerFqdn string

@description('Database name.')
param sqlDatabaseName string

resource storageAccount 'Microsoft.Storage/storageAccounts@2024-01-01' existing = {
  name: storageAccountName
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name:     hostingPlanName
  location: location
  tags:     tags
  kind:     'linux'
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  properties: {
    reserved: true     // required for Linux
  }
}

resource site 'Microsoft.Web/sites@2024-04-01' = {
  name:     functionAppName
  location: location
  // azd needs the `azd-service-name` tag to know which app to deploy to.
  tags:     union(tags, { 'azd-service-name': 'ingest' })
  kind:     'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId:           plan.id
    httpsOnly:              true
    clientAffinityEnabled:  false
    siteConfig: {
      linuxFxVersion:        'Python|3.11'
      ftpsState:             'FtpsOnly'
      minTlsVersion:         '1.2'
      http20Enabled:         true
      pythonVersion:         '3.11'
      cors: {
        allowedOrigins: [ 'https://portal.azure.com' ]
      }
      appSettings: [
        // Functions runtime
        { name: 'FUNCTIONS_EXTENSION_VERSION',                value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME',                   value: 'python' }
        { name: 'AzureWebJobsFeatureFlags',                   value: 'EnableWorkerIndexing' }

        // Identity-based connection for AzureWebJobsStorage (no key in app settings).
        { name: 'AzureWebJobsStorage__accountName',           value: storageAccount.name }
        { name: 'AzureWebJobsStorage__blobServiceUri',        value: storageAccount.properties.primaryEndpoints.blob }
        { name: 'AzureWebJobsStorage__queueServiceUri',       value: storageAccount.properties.primaryEndpoints.queue }
        { name: 'AzureWebJobsStorage__tableServiceUri',       value: storageAccount.properties.primaryEndpoints.table }
        { name: 'AzureWebJobsStorage__credential',            value: 'managedidentity' }

        // App Insights
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING',      value: appInsightsConnString }

        // App config
        { name: 'SOURCE_CONTAINER',                           value: 'source' }
        { name: 'PROCESSED_CONTAINER',                        value: 'processed' }
        { name: 'FAILED_CONTAINER',                           value: 'failed' }
        { name: 'SQL_SERVER',                                 value: sqlServerFqdn }
        { name: 'SQL_DATABASE',                               value: sqlDatabaseName }
        { name: 'LOW_CONFIDENCE_THRESHOLD',                   value: '0.70' }

        // Let Oryx install Python wheels (pyodbc, azure-identity, etc.) on Kudu side.
        // azd uploads source only; remote build resolves requirements.txt.
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT',             value: 'true' }
        { name: 'ENABLE_ORYX_BUILD',                          value: 'true' }
      ]
    }
  }
}

output functionAppName string = site.name
output functionAppId   string = site.id
output principalId     string = site.identity.principalId
output defaultHostName string = site.properties.defaultHostName

// ---------------------------------------------------------------------------
// RBAC — Function MI -> Storage on the storage account.
// The Functions host with identity-based AzureWebJobsStorage needs:
//   * Storage Blob Data Owner       — host secret repository + user blobs
//   * Storage Queue Data Contributor — host scale / lease queues
//   * Storage Table Data Contributor — host instance tables
// Owner (not Contributor) is required so the host can manage azure-webjobs-secrets.
// ---------------------------------------------------------------------------
resource storageBlobDataOwner 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  // Storage Blob Data Owner
  name:  'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
}

resource storageQueueDataContributor 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  // Storage Queue Data Contributor
  name:  '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
}

resource storageTableDataContributor 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  // Storage Table Data Contributor
  name:  '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
}

resource functionMiBlobOwnerRA 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name:  guid(storageAccount.id, functionAppName, storageBlobDataOwner.id)
  properties: {
    principalId:      site.identity.principalId
    principalType:    'ServicePrincipal'
    roleDefinitionId: storageBlobDataOwner.id
  }
}

resource functionMiQueueRA 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name:  guid(storageAccount.id, functionAppName, storageQueueDataContributor.id)
  properties: {
    principalId:      site.identity.principalId
    principalType:    'ServicePrincipal'
    roleDefinitionId: storageQueueDataContributor.id
  }
}

resource functionMiTableRA 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storageAccount
  name:  guid(storageAccount.id, functionAppName, storageTableDataContributor.id)
  properties: {
    principalId:      site.identity.principalId
    principalType:    'ServicePrincipal'
    roleDefinitionId: storageTableDataContributor.id
  }
}
