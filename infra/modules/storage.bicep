// ===========================================================================
// Storage account + containers
// ===========================================================================

@description('Azure region.')
param location string

@description('Tags applied to the storage account.')
param tags object = {}

@description('Globally unique storage account name (3-24 lowercase + digits).')
@minLength(3)
@maxLength(24)
param accountName string

@description('Container names to create.')
param containers array

resource sa 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name:     accountName
  location: location
  tags:     tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion:        'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess:    false
    allowSharedKeyAccess:     true       // Functions runtime currently needs this; identity is still used at the data plane.
    publicNetworkAccess:      'Enabled'
    networkAcls: {
      bypass:        'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2024-01-01' = {
  parent: sa
  name:   'default'
  properties: {
    deleteRetentionPolicy: {
      enabled: true
      days:    7
    }
    containerDeleteRetentionPolicy: {
      enabled: true
      days:    7
    }
  }
}

resource blobContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01' = [for name in containers: {
  parent: blobService
  name:   name
  properties: {
    publicAccess: 'None'
  }
}]

output accountName  string = sa.name
output accountId    string = sa.id
output blobEndpoint string = sa.properties.primaryEndpoints.blob
