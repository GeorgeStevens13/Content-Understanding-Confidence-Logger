// ===========================================================================
// Log Analytics workspace + Application Insights (workspace-based)
// ===========================================================================

@description('Azure region.')
param location string

@description('Tags applied to monitoring resources.')
param tags object = {}

param appInsightsName  string
param logAnalyticsName string

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name:     logAnalyticsName
  location: location
  tags:     tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays:    30
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery:     'Enabled'
  }
}

resource appi 'Microsoft.Insights/components@2020-02-02' = {
  name:     appInsightsName
  location: location
  tags:     tags
  kind:     'web'
  properties: {
    Application_Type:   'web'
    WorkspaceResourceId: law.id
    IngestionMode:      'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery:     'Enabled'
  }
}

output appInsightsName             string = appi.name
output appInsightsConnectionString string = appi.properties.ConnectionString
output logAnalyticsId              string = law.id
