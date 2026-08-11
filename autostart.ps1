<#
Install or remove a per-user Startup shortcut so the stream server is already
running whenever the handheld asks for it. Uses pythonw.exe so nothing shows
on screen; no administrator rights are involved.

    powershell -ExecutionPolicy Bypass -File autostart.ps1 -Install
    powershell -ExecutionPolicy Bypass -File autostart.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Remove
)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup 'PC Monitor.lnk'

if ($Remove) {
    if (Test-Path $link) {
        Remove-Item $link -Force
        "Removed $link"
    } else {
        "Nothing to remove."
    }
    return
}

if (-not $Install) {
    "Pass -Install or -Remove."
    return
}

$exe = python -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"
if (-not (Test-Path $exe)) {
    Write-Error "pythonw.exe not found at $exe"
    return
}

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath = $exe
$sc.Arguments = 'server.py'
$sc.WorkingDirectory = $here
$sc.Description = 'PC Monitor stream server for the Miyoo handheld'
$sc.WindowStyle = 7
$sc.Save()

"Installed $link"
"  target: $exe server.py"
"  workdir: $here"
