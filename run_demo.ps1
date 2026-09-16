[CmdletBinding()]
param(
    [switch]$Gui,
    [switch]$SelfTest,
    [switch]$Mock,
    [switch]$NewRun,
    [switch]$BulkFirstPass,
    [string]$Root,
    [string]$Platform
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function Test-Python312 {
    param([string]$Command, [string[]]$Prefix)
    try {
        $version = & $Command @Prefix -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        return $LASTEXITCODE -eq 0 -and "$version".Trim() -eq "3.12"
    }
    catch {
        return $false
    }
}

function Find-Python312 {
    $bundled = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    $candidates = @(
        @{ Command = "py.exe"; Prefix = @("-3.12") },
        @{ Command = "python3.12.exe"; Prefix = @() },
        @{ Command = "python.exe"; Prefix = @() },
        @{ Command = $bundled; Prefix = @() }
    )
    foreach ($candidate in $candidates) {
        $resolved = Get-Command $candidate.Command -ErrorAction SilentlyContinue
        if ($null -ne $resolved -and (Test-Python312 -Command $resolved.Source -Prefix $candidate.Prefix)) {
            return [pscustomobject]@{ Command = $resolved.Source; Prefix = $candidate.Prefix }
        }
    }
    throw "未找到 Python 3.12。请安装 Python 3.12，并勾选 Add Python to PATH。"
}

function Copy-LocalDependencies {
    param([string]$ScriptRoot, [string]$VenvPython)
    $sourceSite = Join-Path (Split-Path -Parent $ScriptRoot) "ocr_demo_v2\.venv\Lib\site-packages"
    $targetSite = Join-Path $ScriptRoot ".venv\Lib\site-packages"
    $packageItems = @(
        "psycopg",
        "psycopg-3.3.4.dist-info",
        "psycopg_binary",
        "psycopg_binary-3.3.4.dist-info",
        "psycopg_binary.libs",
        "openpyxl",
        "openpyxl-3.1.5.dist-info",
        "et_xmlfile",
        "et_xmlfile-2.0.0.dist-info",
        "typing_extensions.py",
        "typing_extensions-4.16.0.dist-info",
        "tzdata",
        "tzdata-2026.3.dist-info"
    )
    if (-not (Test-Path -LiteralPath $sourceSite)) { return $false }
    foreach ($item in $packageItems) {
        if (-not (Test-Path -LiteralPath (Join-Path $sourceSite $item))) { return $false }
    }
    Write-Host "首次运行：正在复用 ocr_demo_v2 的 Python 依赖（只读原版）..."
    foreach ($item in $packageItems) {
        Copy-Item -LiteralPath (Join-Path $sourceSite $item) -Destination $targetSite -Recurse -Force
    }
    try {
        & $VenvPython -c "import psycopg, openpyxl, PIL; v=tuple(map(int, psycopg.__version__.split('.')[:2])); raise SystemExit(0 if (3, 2) <= v < (4, 0) else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Ensure-Venv {
    param([string]$ScriptRoot)
    $venv = Join-Path $ScriptRoot ".venv"
    $venvPython = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venv)) {
        $python = Find-Python312
        Write-Host "首次运行：正在创建 Python 3.12 虚拟环境..."
        $arguments = @($python.Prefix) + @("-m", "venv", $venv)
        & $python.Command @arguments
        if ($LASTEXITCODE -ne 0) { throw "创建虚拟环境失败。" }
    }
    if (-not (Test-Path -LiteralPath $venvPython) -or -not (Test-Python312 -Command $venvPython -Prefix @())) {
        throw "现有 .venv 不是可用的 Python 3.12 环境；为保护文件，脚本不会自动删除它。"
    }

    $dependenciesReady = $false
    try {
        & $venvPython -c "import psycopg, openpyxl, PIL; v=tuple(map(int, psycopg.__version__.split('.')[:2])); raise SystemExit(0 if (3, 2) <= v < (4, 0) else 1)" 2>$null
        $dependenciesReady = $LASTEXITCODE -eq 0
    }
    catch {
        $dependenciesReady = $false
    }
    if (-not $dependenciesReady) {
        $dependenciesReady = Copy-LocalDependencies -ScriptRoot $ScriptRoot -VenvPython $venvPython
    }
    if (-not $dependenciesReady) {
        Write-Host "首次运行：正在从 Python 官方源安装 psycopg、openpyxl 和 Pillow..."
        try {
            $null = & $venvPython -m pip install --disable-pip-version-check --retries 5 --timeout 120 "psycopg[binary]>=3.2,<4" "openpyxl>=3.1,<4" "Pillow>=10,<13"
            $dependenciesReady = $LASTEXITCODE -eq 0
        }
        catch {
            $dependenciesReady = $false
        }
    }
    if (-not $dependenciesReady) {
        throw "安装 Python 依赖失败：本地原测试版无可复用依赖，且 Python 官方源下载失败。"
    }
    return $venvPython
}

function Select-BatchRoot {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = [System.Windows.Forms.FolderBrowserDialog]::new()
    $dialog.Description = "请选择批次根目录（平台/批次/product_id/图片）"
    $dialog.ShowNewFolderButton = $false
    try {
        if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            return $dialog.SelectedPath
        }
        return $null
    }
    finally {
        $dialog.Dispose()
    }
}

try {
    if ($SelfTest -and $Mock) { throw "-SelfTest 与 -Mock 不能同时使用。" }
    $scriptRoot = $PSScriptRoot
    $demo = Join-Path $scriptRoot "demo.py"
    if (-not (Test-Path -LiteralPath $demo)) { throw "缺少 demo.py。" }

    $venvPython = Ensure-Venv -ScriptRoot $scriptRoot
    if ($Gui) {
        & $venvPython (Join-Path $scriptRoot "直接用图片测试.py")
        if ($LASTEXITCODE -ne 0) { throw "Demo 前端运行失败（退出码 $LASTEXITCODE）。" }
        exit 0
    }

    $arguments = @($demo)
    if ($SelfTest) {
        $arguments += "--self-test"
    }
    elseif ($Mock) {
        $arguments += "--mock"
        if ($Root) { $arguments += @("--root", $Root) }
    }
    else {
        if (-not $Root) { $Root = Select-BatchRoot }
        if (-not $Root) {
            Write-Host "已取消。"
            exit 0
        }
        $rootItem = Get-Item -LiteralPath $Root -ErrorAction Stop
        if (-not $rootItem.PSIsContainer) { throw "-Root 必须是目录。" }
        $arguments += @("--root", $rootItem.FullName)
    }
    if ($Platform) { $arguments += @("--platform", $Platform) }
    if ($NewRun) { $arguments += "--new-run" }
    if ($BulkFirstPass) { $arguments += "--bulk-first-pass" }
    if (-not $SelfTest) { $arguments += "--open" }

    & $venvPython @arguments
    if ($LASTEXITCODE -ne 0) { throw "Demo 运行失败（退出码 $LASTEXITCODE）。" }
}
catch {
    Write-Host ""
    Write-Host "错误：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
