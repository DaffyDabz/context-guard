<#
.SYNOPSIS
  Installs (or removes) the Context Guard hooks and status line in the user-level Claude Code settings
  (%USERPROFILE%\.claude\settings.json), so they apply to every project.

.DESCRIPTION
  Backs up settings.json next to itself as settings.json.before-context-guard-<timestamp>, then merges:
    hooks.SessionStart      -> context_guard.py hook  (budget reminder injected into the model)
    hooks.UserPromptSubmit  -> context_guard.py hook  (warn, or refuse the prompt when over budget)
    hooks.PreToolUse        -> context_guard.py hook  (warn, or refuse the tool call when over budget)
    hooks.PreToolUse        -> output_trim.py         (matcher Bash|PowerShell; skipped with -NoOutputTrim)
    statusLine              -> context_guard.py statusline
  Existing hooks from other sources are preserved; the guard's own entries are recognised by the script path and
  replaced, so re-running is idempotent. -Uninstall removes only the guard's entries.

.PARAMETER Python
  Full path to python.exe (3.8+). Defaults to whatever `python` (or the `py` launcher) resolves to on PATH.
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$NoOutputTrim,
    [string]$SettingsPath = (Join-Path $env:USERPROFILE '.claude\settings.json'),
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'context_guard.py'
$trimScript = Join-Path $PSScriptRoot 'output_trim.py'
if (-not (Test-Path -LiteralPath $script)) { throw "context_guard.py not found next to this installer" }
if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command py -ErrorAction SilentlyContinue }
    if ($cmd) { $Python = $cmd.Source }
}
if (-not $Uninstall -and (-not $Python -or -not (Test-Path -LiteralPath $Python))) { throw "Python not found. Pass -Python <full path to python.exe>" }   # -Uninstall works without Python
$hookCmd = ('"{0}" "{1}" hook' -f $Python, $script)
$statusCmd = ('"{0}" "{1}" statusline' -f $Python, $script)
$trimCmd = ('"{0}" "{1}"' -f $Python, $trimScript)

$SettingsPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($SettingsPath)   # .NET file calls need a full path
$exists = Test-Path -LiteralPath $SettingsPath
if ($Uninstall -and -not $exists) { Write-Host "No settings file at $SettingsPath; nothing to remove."; return }
$raw = '{}'
if ($exists) { $raw = [IO.File]::ReadAllText($SettingsPath) }
if (-not $raw.Trim()) { $raw = '{}' }   # an empty file counts as empty settings
$cfg = $raw | ConvertFrom-Json
if ($exists) {
    $backup = $SettingsPath + '.before-context-guard-' + (Get-Date).ToString('yyyyMMdd-HHmmss')
    Copy-Item -LiteralPath $SettingsPath -Destination $backup -Force
    Write-Host "backup: $backup"
} else {
    $dir = Split-Path -Parent $SettingsPath
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}

function Strip-GuardEntries($groups) {
    # remove any hook group whose commands all point at context_guard.py; keep others untouched
    $kept = @()
    foreach ($g in @($groups)) {
        if ($null -eq $g) { continue }
        $cmds = @($g.hooks | ForEach-Object { [string]$_.command })
        $isGuard = ($cmds.Count -gt 0) -and (@($cmds | Where-Object { $_ -like '*context_guard.py*' -or $_ -like '*output_trim.py*' }).Count -eq $cmds.Count)
        if (-not $isGuard) { $kept += $g }
    }
    return ,$kept
}

if (-not ($cfg.PSObject.Properties.Name -contains 'hooks') -or $null -eq $cfg.hooks) { $cfg | Add-Member -NotePropertyName hooks -NotePropertyValue ([pscustomobject]@{}) -Force }
foreach ($ev in 'SessionStart', 'UserPromptSubmit', 'PreToolUse') {
    $existing = @()
    if ($cfg.hooks.PSObject.Properties.Name -contains $ev) { $existing = Strip-GuardEntries $cfg.hooks.$ev }
    if (-not $Uninstall) {
        $entry = [pscustomobject]@{ hooks = @([pscustomobject]@{ type = 'command'; command = $hookCmd; timeout = 20 }) }
        $existing = @($existing) + @($entry)
        if ($ev -eq 'PreToolUse' -and -not $NoOutputTrim) {
            $trim = [pscustomobject]@{ matcher = 'Bash|PowerShell'; hooks = @([pscustomobject]@{ type = 'command'; command = $trimCmd; timeout = 10 }) }
            $existing = @($existing) + @($trim)
        }
    }
    if ($existing.Count -gt 0 -and ($cfg.hooks.PSObject.Properties.Name -contains $ev)) { $cfg.hooks.$ev = @($existing) }   # keeps key order
    elseif ($existing.Count -gt 0) { $cfg.hooks | Add-Member -NotePropertyName $ev -NotePropertyValue @($existing) -Force }
    elseif ($cfg.hooks.PSObject.Properties.Name -contains $ev) { $cfg.hooks.PSObject.Properties.Remove($ev) }
}
if (@($cfg.hooks.PSObject.Properties).Count -eq 0) { $cfg.PSObject.Properties.Remove('hooks') }

if ($Uninstall) {
    if (($cfg.PSObject.Properties.Name -contains 'statusLine') -and ([string]$cfg.statusLine.command -like '*context_guard.py*')) { $cfg.PSObject.Properties.Remove('statusLine') }
} else {
    $cfg | Add-Member -NotePropertyName statusLine -NotePropertyValue ([pscustomobject]@{ type = 'command'; command = $statusCmd }) -Force
}

$out = $cfg | ConvertTo-Json -Depth 20
[IO.File]::WriteAllText($SettingsPath, $out, (New-Object System.Text.UTF8Encoding($false)))
$check = [IO.File]::ReadAllText($SettingsPath) | ConvertFrom-Json
Write-Host ("settings written: {0}" -f $SettingsPath)
Write-Host ("  hooks: {0}" -f (@($check.hooks.PSObject.Properties.Name) -join ', '))
Write-Host ("  statusLine: {0}" -f $(if ($check.statusLine) { $check.statusLine.command } else { '(none)' }))
Write-Host $(if ($Uninstall) { 'Context Guard REMOVED. Restart Claude Code sessions to apply.' } else { 'Context Guard INSTALLED. New Claude Code sessions pick it up; the current session may need a restart.' })
