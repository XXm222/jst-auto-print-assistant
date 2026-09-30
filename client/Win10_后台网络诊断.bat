@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

set "CONFIG_PATH=%~dp0jst_operator_config.json"
if not exist "%CONFIG_PATH%" set "CONFIG_PATH=%LOCALAPPDATA%\Programs\JSTAutoPrint\jst_operator_config.json"
set "TEMP_PS=%TEMP%\JSTBackendDiag_%RANDOM%_%RANDOM%.ps1"
set "PAYLOAD_LINE="
for /f "tokens=1 delims=:" %%N in ('findstr /n /c:"# POWERSHELL PAYLOAD" "%~f0"') do set "PAYLOAD_LINE=%%N"
if not defined PAYLOAD_LINE (
  echo ERROR: diagnostic payload was not found.
  pause
  exit /b 3
)

more +%PAYLOAD_LINE% "%~f0" > "%TEMP_PS%"
if errorlevel 1 (
  echo ERROR: failed to prepare the diagnostic payload.
  pause
  exit /b 4
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP_PS%" -ConfigPath "%CONFIG_PATH%"
set "DIAG_RC=%ERRORLEVEL%"
del /q "%TEMP_PS%" >nul 2>&1
if not "%DIAG_RC%"=="0" pause
exit /b %DIAG_RC%

# POWERSHELL PAYLOAD
﻿[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,
    [ValidateRange(1, 5)]
    [int]$Attempts = 3
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
$DiagnosticRoot = Join-Path ([Environment]::GetFolderPath("UserProfile")) ".jst-auto-print\diagnostics"
$LogPath = Join-Path $DiagnosticRoot ("后台网络诊断_{0}.txt" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
$Lines = New-Object "Collections.Generic.List[string]"
$ResponseTimes = New-Object "Collections.Generic.List[double]"
$Passed = 0
$FailureCategory = ""
$FailureReason = ""

function Add-LogLine {
    param([string]$Text)
    $Lines.Add(("[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $Text))
}

function Get-DeepestMessage {
    param([Exception]$Exception)
    $current = $Exception
    while ($null -ne $current.InnerException) { $current = $current.InnerException }
    return [string]$current.Message
}

function Classify-Failure {
    param([string]$Message)
    if ($Message -match "(?i)(name resolution|no such host|无法解析|找不到.*主机|DNS)") { return "DNS 解析失败" }
    if ($Message -match "(?i)(certificate|SSL|TLS|trust|secure channel|证书|安全通道|信任)") { return "HTTPS/TLS 证书或安全软件拦截" }
    if ($Message -match "(?i)(timed out|timeout|task was canceled|超时)") { return "网络请求超时" }
    if ($Message -match "(?i)(refused|forcibly closed|connection reset|拒绝|强迫关闭|重置)") { return "连接被防火墙、代理或远端拒绝" }
    return "网络请求失败"
}

function Show-Result {
    param([string]$Message, [bool]$Succeeded)
    Write-Host ""
    Write-Host $Message
    Write-Host ("完整日志：{0}" -f $LogPath)
    if ($env:JST_DIAG_NO_POPUP -eq "1") { return }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $icon = if ($Succeeded) { 64 } else { 16 }
        [void]$shell.Popup(($Message + "`r`n`r`n完整日志：" + $LogPath), 0, "聚水潭后台网络诊断", $icon)
    }
    catch { Write-Host ("无法显示结果弹窗：{0}" -f $_.Exception.Message) }
}

try {
    if (-not (Test-Path -LiteralPath $DiagnosticRoot -PathType Container)) {
        New-Item -ItemType Directory -Path $DiagnosticRoot -Force | Out-Null
    }
    Add-LogLine "聚水潭安全打单助手 V0.5.25 后台网络诊断开始"
    Add-LogLine ("Windows 时间：{0}" -f (Get-Date).ToString("yyyy-MM-dd HH:mm:ss zzz"))

    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "没有找到 jst_operator_config.json；请把本 BAT 放到软件安装目录后再运行"
    }
    try { $config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw ("部署配置无法读取：{0}" -f $_.Exception.Message) }

    $apiUrl = [string]$config.api_url
    $apiToken = [string]$config.api_token
    $uri = $null
    if (-not [Uri]::TryCreate($apiUrl, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -ne "https") {
        throw "部署配置中的后台地址不是有效 HTTPS 地址"
    }
    if ($apiToken -notmatch "^[A-Za-z0-9_-]{32,128}$") { throw "部署配置中的 API 凭证格式无效" }
    $pingUri = [Uri]::new($apiUrl.TrimEnd("/") + "/ping")
    Add-LogLine ("配置：正常；API={0}；凭证已设置（内容不写入日志）" -f $apiUrl)

    try {
        $proxy = [Net.WebRequest]::DefaultWebProxy
        $proxyUri = if ($null -ne $proxy) { $proxy.GetProxy($pingUri) } else { $pingUri }
        if ($null -ne $proxyUri -and $proxyUri.AbsoluteUri -ne $pingUri.AbsoluteUri) {
            Add-LogLine ("系统代理：{0}://{1}:{2}" -f $proxyUri.Scheme, $proxyUri.Host, $proxyUri.Port)
        }
        else { Add-LogLine "系统代理：未检测到显式代理" }
    }
    catch { Add-LogLine ("系统代理：读取失败（继续诊断）：{0}" -f $_.Exception.Message) }

    try {
        $addresses = @([Net.Dns]::GetHostAddresses($pingUri.DnsSafeHost) | ForEach-Object { $_.IPAddressToString } | Sort-Object -Unique)
        if ($addresses.Count -eq 0) { throw "DNS 未返回地址" }
        Add-LogLine ("DNS：正常；{0} -> {1}" -f $pingUri.DnsSafeHost, ($addresses -join ", "))
    }
    catch {
        $FailureCategory = "DNS 解析失败"
        $FailureReason = Get-DeepestMessage $_.Exception
        throw ("{0}：{1}" -f $FailureCategory, $FailureReason)
    }

    $port = if ($pingUri.IsDefaultPort) { 443 } else { $pingUri.Port }
    $tcp = New-Object Net.Sockets.TcpClient
    $connect = $null
    try {
        $connect = $tcp.BeginConnect($pingUri.DnsSafeHost, $port, $null, $null)
        if (-not $connect.AsyncWaitHandle.WaitOne(5000)) { throw "连接 5 秒超时" }
        $tcp.EndConnect($connect)
        Add-LogLine ("TCP：正常；{0}:{1} 可连接" -f $pingUri.DnsSafeHost, $port)
    }
    catch {
        $FailureCategory = "TCP 443 连接失败"
        $FailureReason = Get-DeepestMessage $_.Exception
        throw ("{0}：{1}" -f $FailureCategory, $FailureReason)
    }
    finally {
        if ($null -ne $connect) { $connect.AsyncWaitHandle.Close() }
        $tcp.Close()
    }

    Add-Type -AssemblyName System.Net.Http
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(10)
    try {
        for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
            $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Post, $pingUri)
            $request.Headers.Authorization = [Net.Http.Headers.AuthenticationHeaderValue]::new("Bearer", $apiToken)
            [void]$request.Headers.UserAgent.ParseAdd("JSTAutoPrint/0.5.25")
            $request.Content = [Net.Http.StringContent]::new("{}", [Text.Encoding]::UTF8, "application/json")
            $response = $null
            $watch = [Diagnostics.Stopwatch]::StartNew()
            try {
                $response = $client.SendAsync($request).GetAwaiter().GetResult()
                $watch.Stop()
                $milliseconds = [Math]::Round($watch.Elapsed.TotalMilliseconds)
                $statusCode = [int]$response.StatusCode
                $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
                if ($statusCode -ge 300 -and $statusCode -lt 400) {
                    throw ("后台返回重定向 HTTP {0}，Location={1}" -f $statusCode, [string]$response.Headers.Location)
                }
                if ($statusCode -ne 200) { throw ("后台返回 HTTP {0}" -f $statusCode) }
                try { $payload = $body | ConvertFrom-Json }
                catch { throw "后台返回 HTTP 200，但内容不是有效 JSON" }
                if ($payload.ok -ne $true) { throw "后台健康检查没有返回 ok=true" }
                if ([int]$payload.api_schema_version -ne 5) { throw ("后台 API schema 不兼容：{0}" -f $payload.api_schema_version) }
                if ([string]$payload.minimum_client_version -notmatch "^\d+\.\d+\.\d+$") { throw "后台没有返回有效最低客户端版本" }
                if ($payload.lease_required -ne $true) { throw "后台未启用强制租约" }
                $Passed += 1
                $ResponseTimes.Add($milliseconds)
                Add-LogLine ("HTTPS /ping 第 {0}/{1} 次：HTTP 200，{2} ms，ok=true，schema=5，最低客户端={3}" -f $attempt, $Attempts, $milliseconds, $payload.minimum_client_version)
                if ($null -ne $response.Headers.Date) {
                    $skew = [Math]::Round([Math]::Abs(((Get-Date).ToUniversalTime() - $response.Headers.Date.UtcDateTime).TotalSeconds))
                    Add-LogLine ("服务器 Date 与本机 UTC 偏差约 {0} 秒" -f $skew)
                }
            }
            catch {
                $watch.Stop()
                $reason = Get-DeepestMessage $_.Exception
                $category = if ($reason -match "^后台返回 HTTP") { "后台 HTTP 状态异常" } elseif ($reason -match "重定向") { "后台发生重定向" } elseif ($reason -match "HTTP 200") { "后台响应格式异常" } else { Classify-Failure $reason }
                if (-not $FailureCategory) { $FailureCategory = $category; $FailureReason = $reason }
                Add-LogLine ("HTTPS /ping 第 {0}/{1} 次：失败；{2}；{3}" -f $attempt, $Attempts, $category, $reason)
            }
            finally {
                $request.Dispose()
                if ($null -ne $response) { $response.Dispose() }
            }
            if ($attempt -lt $Attempts) { Start-Sleep -Seconds 1 }
        }
    }
    finally { $client.Dispose(); $handler.Dispose() }
}
catch {
    if (-not $FailureReason) { $FailureReason = Get-DeepestMessage $_.Exception }
    if (-not $FailureCategory) { $FailureCategory = "诊断未能完成" }
    Add-LogLine ("诊断中止：{0}" -f $_.Exception.Message)
}

if ($Passed -eq $Attempts) {
    $average = [Math]::Round(($ResponseTimes | Measure-Object -Average).Average)
    $result = "后台基础连接正常`r`n${Passed}/${Attempts} 次 /ping 请求成功，平均响应 ${average} ms。`r`n本检查不会调用可能领取订单的 /plan；若软件仍报网络异常，通常是后台计划任务内部故障，请把日志交给维护人员。"
    $exitCode = 0
}
elseif ($Passed -gt 0) {
    $result = "后台网络不稳定`r`n${Passed}/${Attempts} 次请求成功。`r`n主要失败类型：${FailureCategory}`r`n原因：${FailureReason}"
    $exitCode = 1
}
else {
    $result = "后台 API 请求失败`r`n0/${Attempts} 次请求成功。`r`n故障类型：${FailureCategory}`r`n原因：${FailureReason}"
    $exitCode = 2
}

Add-LogLine ($result -replace "`r?`n", "；")
$Lines | Set-Content -LiteralPath $LogPath -Encoding UTF8
Show-Result -Message $result -Succeeded ($exitCode -eq 0)
exit $exitCode
