#Requires -RunAsAdministrator
# Install the build into Program Files and put the shortcuts on the desktop
# and into the Start menu (for all users).
$ErrorActionPreference = "Stop"

$installDir = Join-Path $env:ProgramFiles "Corridoor_app"   # same as in build.ps1
$dist = Join-Path $PSScriptRoot "corridoor.dist"
if (-not (Test-Path (Join-Path $dist "corridoor.exe"))) { throw "Run build.ps1 first." }

# Replace only the program; the encrypted settings live in another folder.
if (Test-Path $installDir) { Remove-Item -Recurse -Force $installDir }
Copy-Item -Recurse $dist $installDir

$targets = @(
    [Environment]::GetFolderPath("CommonDesktopDirectory"),
    [Environment]::GetFolderPath("CommonPrograms")
)
foreach ($target in $targets) {
    Copy-Item -Force (Join-Path $installDir "*.lnk") $target
}
Write-Host "Installed to $installDir"
