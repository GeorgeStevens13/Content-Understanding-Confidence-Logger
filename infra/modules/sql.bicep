// ===========================================================================
// Azure SQL — server (Entra-only auth) + single database
// ===========================================================================

@description('Azure region.')
param location string

@description('Tags applied to all SQL resources.')
param tags object = {}

@description('Logical SQL server name.')
param sqlServerName string

@description('Database name.')
param sqlDatabaseName string

@description('SKU for the database.')
param sqlDatabaseSku string = 'S0'

@description('Object ID of the Entra principal to set as SQL admin.')
param adminObjectId string

@description('Display name / UPN of the Entra admin.')
param adminLogin string

@allowed([ 'User', 'Group', 'Application' ])
@description('Type of Entra principal for the admin.')
param adminPrincipalType string = 'User'

resource server 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name:     sqlServerName
  location: location
  tags:     tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    version:                       '12.0'
    publicNetworkAccess:           'Enabled'
    minimalTlsVersion:             '1.2'
    administrators: {
      administratorType:           'ActiveDirectory'
      principalType:               adminPrincipalType
      login:                       adminLogin
      sid:                         adminObjectId
      tenantId:                    subscription().tenantId
      azureADOnlyAuthentication:   true
    }
  }
}

// Allow Azure services (Functions outbound) to reach the server.
resource fwAzure 'Microsoft.Sql/servers/firewallRules@2023-08-01-preview' = {
  parent: server
  name:   'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress:   '0.0.0.0'
  }
}

resource db 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  parent:   server
  name:     sqlDatabaseName
  location: location
  tags:     tags
  sku: {
    name: sqlDatabaseSku
  }
  properties: {
    collation:   'SQL_Latin1_General_CP1_CI_AS'
    zoneRedundant: false
  }
}

output serverFqdn        string = server.properties.fullyQualifiedDomainName
output serverName        string = server.name
output databaseName      string = db.name
