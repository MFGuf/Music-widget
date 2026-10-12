<#
.SYNOPSIS
    Регистрирует native messaging хост Music Widget в Firefox (Windows).

.DESCRIPTION
    Firefox ищет манифест хоста в реестре:
        HKEY_CURRENT_USER\Software\Mozilla\NativeMessagingHosts\<name>

    Имя хоста - это поле "name" в music_widget_host.json, а сам манифест
    копируется в %LOCALAPPDATA%\MusicWidget\host.json, где в поле "path"
    стоит АБСОЛЮТНЫЙ путь к host_launcher.bat. Так репозиторий можно
    перемещать, а в реестр попадает всегда рабочий путь.

.PARAMETER Uninstall
    Удаляет запись из реестра и временный манифест.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File native\install_host.ps1
#>
[CmdletBinding()]
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'

$HostName  = 'music_widget_host'
$NativeDir = $PSScriptRoot
$Launcher  = Join-Path $NativeDir 'host_launcher.bat'
$Manifest  = Join-Path $NativeDir 'music_widget_host.json'
$HostPy    = Join-Path $NativeDir 'host.py'
$InstallDir = Join-Path $env:LOCALAPPDATA 'MusicWidget'
$Installed  = Join-Path $InstallDir 'host.json'
$RegKey     = "HKCU:\Software\Mozilla\NativeMessagingHosts\$HostName"

function Remove-HostRegistration {
    if (Test-Path $RegKey) {
        Remove-Item -Path $RegKey -Recurse -Force
        Write-Host ('[OK] Запись реестра удалена: HKCU\Software\Mozilla\NativeMessagingHosts\{0}' -f $HostName)
    } else {
        Write-Host '[--] Записи в реестре нет'
    }
    if (Test-Path $InstallDir) {
        Remove-Item -Path $InstallDir -Recurse -Force
        Write-Host '[OK] Удалён временный каталог:' $InstallDir
    }
}

if ($Uninstall) {
    Remove-HostRegistration
    Write-Host ''
    Write-Host 'Хост удалён. Расширение будет показывать "нет связи с виджетом".'
    exit 0
}

# --- 1. Проверки ------------------------------------------------------------
if (-not (Test-Path $Launcher))  { throw "Не найден лаунчер: $Launcher" }
if (-not (Test-Path $Manifest)) { throw "Не найден манифест хоста: $Manifest" }

if (-not (Get-Command python.exe -ErrorAction SilentlyContinue)) {
    throw 'Python не найден в PATH. Установите Python 3.10+ или задайте MUSIC_WIDGET_PYTHON.'
}

if (-not (Test-Path $HostPy)) {
    Write-Warning 'host.py ещё не создан (это задача 17) - регистрация пройдёт, но хост пока не запустится.'
}

# --- 2. Готовим манифест с абсолютным путём ---------------------------------
$spec = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
$spec.path = $Launcher

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
# Без -Depth JSON остаётся плоским - так требует Firefox.
$spec | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Installed -Encoding UTF8

# Firefox требует, чтобы манифест хоста читался как UTF-8 без BOM.
$text = [System.IO.File]::ReadAllText($Installed)
[System.IO.File]::WriteAllText($Installed, $text, (New-Object System.Text.UTF8Encoding $false))

Write-Host '[OK] Манифест установлен:' $Installed
Write-Host '     path =' $Launcher

# --- 3. Пишем в реестр -------------------------------------------------------
New-Item -Path $RegKey -Force | Out-Null
Set-Item -Path $RegKey -Value $Installed

Write-Host ('[OK] Зарегистрирован хост: {0}' -f $HostName)
Write-Host ('     HKCU\Software\Mozilla\NativeMessagingHosts\{0}' -f $HostName)

# --- 4. Что дальше -----------------------------------------------------------
Write-Host ''
Write-Host '  Следующие шаги'
Write-Host '  =============='
Write-Host '  1. Установить расширение (нужна постоянная установка):'
Write-Host '       python tools\build_xpi.py'
Write-Host '       затем about:debugging -> Загрузить временное дополнение -> dist\music-widget.xpi'
Write-Host '       (для постоянной установки подпишите XPI на addons.mozilla.org,'
Write-Host '        либо используйте Firefox Developer Edition / Nightly / ESR)'
Write-Host '  2. Перезапустите Firefox - хост подхватится при старте.'
Write-Host '  3. Запустите виджет:  run.bat'
Write-Host ''
Write-Host '  ID расширения: music-widget@local.dev'
Write-Host '  Имя хоста:     ' $HostName
Write-Host ''
Write-Host '  Удалить регистрацию:'
Write-Host '       powershell -ExecutionPolicy Bypass -File native\install_host.ps1 -Uninstall'