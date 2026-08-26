# SPDX-License-Identifier: MIT
$ErrorActionPreference = "Stop"

$Gateway = (
    Get-NetRoute -DestinationPrefix "0.0.0.0/0" |
        Sort-Object RouteMetric |
        Select-Object -First 1
).NextHop
$BaseUrl = "http://${Gateway}:8766"
$Archive = "C:\oem.tar.gz"

Invoke-WebRequest -UseBasicParsing "${BaseUrl}/oem.tar.gz" -OutFile $Archive
$ExpectedHash = (Invoke-RestMethod -UseBasicParsing "${BaseUrl}/oem.tar.gz.sha256").Trim().Split()[0].ToLower()
$ActualHash = (Get-FileHash $Archive -Algorithm SHA256).Hash.ToLower()
if ($ActualHash -ne $ExpectedHash) {
    throw "Wayseam OEM archive hash mismatch"
}

Set-Location C:\
New-Item -ItemType Directory -Path C:\OEM -Force | Out-Null
tar -xzf $Archive
& C:\OEM\install.bat
