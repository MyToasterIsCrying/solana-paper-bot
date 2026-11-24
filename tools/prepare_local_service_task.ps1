$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $env:LOCALAPPDATA 'SolanaPaperBot'
$serviceTemplatePath = Join-Path $PSScriptRoot 'local-service-task.xml.template'
$serviceOutputPath = Join-Path $dataDir 'local-service-task.disabled.xml'
$publisherTemplatePath = Join-Path $PSScriptRoot 'dashboard-publisher-task.xml.template'
$publisherOutputPath = Join-Path $dataDir 'dashboard-publisher-task.disabled.xml'
$userSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$powerShellPath = Join-Path $PSHOME 'powershell.exe'
$runnerPath = Join-Path $PSScriptRoot 'run_local_service_task.ps1'
$pythonPath = Join-Path $dataDir 'venv\Scripts\python.exe'
$publisherPath = Join-Path $PSScriptRoot 'publish_dashboard.py'

$serviceXml = Get-Content $serviceTemplatePath -Raw -Encoding UTF8
$serviceXml = $serviceXml.Replace('@@USER_SID@@', [System.Security.SecurityElement]::Escape($userSid))
$serviceXml = $serviceXml.Replace('@@POWERSHELL_PATH@@', [System.Security.SecurityElement]::Escape($powerShellPath))
$serviceXml = $serviceXml.Replace('@@RUNNER_PATH@@', [System.Security.SecurityElement]::Escape($runnerPath))
$serviceXml = $serviceXml.Replace('@@REPOSITORY_PATH@@', [System.Security.SecurityElement]::Escape($repoRoot))

$publisherXml = Get-Content $publisherTemplatePath -Raw -Encoding UTF8
$publisherXml = $publisherXml.Replace('@@USER_SID@@', [System.Security.SecurityElement]::Escape($userSid))
$publisherXml = $publisherXml.Replace('@@START_BOUNDARY@@', (Get-Date).AddMinutes(1).ToString('s'))
$publisherXml = $publisherXml.Replace('@@PYTHON_PATH@@', [System.Security.SecurityElement]::Escape($pythonPath))
$publisherXml = $publisherXml.Replace('@@PUBLISHER_PATH@@', [System.Security.SecurityElement]::Escape($publisherPath))
$publisherXml = $publisherXml.Replace('@@REPOSITORY_PATH@@', [System.Security.SecurityElement]::Escape($repoRoot))

[xml]$parsedService = $serviceXml
[xml]$parsedPublisher = $publisherXml
foreach ($taskXml in @($serviceXml, $publisherXml)) {
    if (-not $taskXml.Contains('<Enabled>false</Enabled>') -or
        -not $taskXml.Contains('<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>') -or
        -not $taskXml.Contains('<RestartOnFailure>')) {
        throw 'Generated task definition is missing a required safety setting.'
    }
}

New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($serviceOutputPath, $serviceXml, $utf8)
[System.IO.File]::WriteAllText($publisherOutputPath, $publisherXml, $utf8)
Write-Output "Wrote disabled service task definition: $serviceOutputPath"
Write-Output "Wrote disabled publisher task definition: $publisherOutputPath"
Write-Output 'The task was not registered or enabled.'
