// =============================================================================
// Content Understanding Confidence Logger — infra
// Resource-group scope. Provisions storage (with 3 containers), Azure SQL
// (Entra-only admin), a Python Linux Consumption Function App, App Insights,
// and the RBAC needed for the Function's MI to read/write storage.
// SQL RBAC for the Function's MI is a manual post-deploy step (see sql/README.md).
// =============================================================================

targetScope = 'resourceGroup'

// ---------- params -----------------------------------------------------------

@description('Short environment name used in resource names (e.g. dev, test, prod).')
@minLength(2)
@maxLength(10)
param environmentName string

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Object ID of the Entra ID principal that will be the SQL admin.  Defaults to the current az login user — set via azd up.')
param sqlAdminObjectId string

@description('Display name (UPN) of the Entra ID principal that will be the SQL admin.')
param sqlAdminLogin string

@description('Whether the SQL admin principal is a user (default) or a service principal.')
@allowed([ 'User', 'Group', 'Application' ])
param sqlAdminPrincipalType string = 'User'

@description('Name of the Azure SQL database created on the server.')
param sqlDatabaseName string = 'cu_confidence'

@description('SKU name for the SQL database (e.g. Basic, S0, S1, GP_S_Gen5_1).')
param sqlDatabaseSku string = 'S0'

@description('Resource tags applied to every resource.')
param tags object = {
  'azd-env-name':  environmentName
  workload:        'cu-confidence-logger'
}

// ---------- names ------------------------------------------------------------

var rgUnique           = uniqueString(subscription().id, resourceGroup().id)
var rgUniqueShort      = substring(rgUnique, 0, 8)

var storageAccountName = toLower('stcuc${environmentName}${rgUniqueShort}')
var sqlServerName      = toLower('sql-cuc-${environmentName}-${rgUniqueShort}')
var functionAppName    = toLower('func-cuc-${environmentName}-${rgUniqueShort}')
var hostingPlanName    = toLower('plan-cuc-${environmentName}-${rgUniqueShort}')
var appInsightsName    = toLower('appi-cuc-${environmentName}-${rgUniqueShort}')
var logAnalyticsName   = toLower('log-cuc-${environmentName}-${rgUniqueShort}')

// ---------- modules ----------------------------------------------------------

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location:           location
    tags:               tags
    appInsightsName:    appInsightsName
    logAnalyticsName:   logAnalyticsName
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    location:    location
    tags:        tags
    accountName: storageAccountName
    containers:  [ 'source', 'processed', 'failed' ]
  }
}

module sql 'modules/sql.bicep' = {
  name: 'sql'
  params: {
    location:              location
    tags:                  tags
    sqlServerName:         sqlServerName
    sqlDatabaseName:       sqlDatabaseName
    sqlDatabaseSku:        sqlDatabaseSku
    adminObjectId:         sqlAdminObjectId
    adminLogin:            sqlAdminLogin
    adminPrincipalType:    sqlAdminPrincipalType
  }
}

module function 'modules/function.bicep' = {
  name: 'function'
  params: {
    location:              location
    tags:                  tags
    functionAppName:       functionAppName
    hostingPlanName:       hostingPlanName
    storageAccountName:    storage.outputs.accountName
    appInsightsConnString: monitoring.outputs.appInsightsConnectionString
    sqlServerFqdn:         sql.outputs.serverFqdn
    sqlDatabaseName:       sqlDatabaseName
  }
}

// ---------- outputs ----------------------------------------------------------

output AZURE_LOCATION             string = location
output AZURE_RESOURCE_GROUP       string = resourceGroup().name

output STORAGE_ACCOUNT_NAME       string = storage.outputs.accountName
output SOURCE_CONTAINER           string = 'source'
output PROCESSED_CONTAINER        string = 'processed'
output FAILED_CONTAINER           string = 'failed'

output FUNCTION_APP_NAME          string = function.outputs.functionAppName
output FUNCTION_PRINCIPAL_ID      string = function.outputs.principalId

output SQL_SERVER                 string = sql.outputs.serverFqdn
output SQL_DATABASE               string = sqlDatabaseName

output APPLICATIONINSIGHTS_NAME   string = appInsightsName
