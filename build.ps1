# Build the program, its icons and the shortcuts (run on Windows).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# The shortcuts point to the installation folder used by install.ps1.
$installDir = Join-Path $env:ProgramFiles "Corridoor_app"
$dist = Join-Path $PSScriptRoot "corridoor.dist"

python make_icons.py
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }

python -m nuitka --mode=standalone --windows-uac-admin --windows-console-mode=disable `
    --enable-plugin=tk-inter --assume-yes-for-downloads `
    --windows-icon-from-ico=run.ico --output-filename=corridoor.exe corridoor.py
if ($LASTEXITCODE -ne 0) { throw "Nuitka build failed." }

Copy-Item run.ico, hop.ico, config.ico $dist

$shell = New-Object -ComObject WScript.Shell
$links = @(
    @{ Name = "Corridoor SSH";          Args = "";         Icon = "run.ico" },
    @{ Name = "Corridoor HopToDesk";    Args = "--hoptodesk"; Icon = "hop.ico" },
    @{ Name = "Corridoor Settings";     Args = "--config"; Icon = "config.ico" }
)
foreach ($link in $links) {
    $shortcut = $shell.CreateShortcut((Join-Path $dist "$($link.Name).lnk"))
    $shortcut.TargetPath = Join-Path $installDir "corridoor.exe"
    $shortcut.Arguments = $link.Args
    $shortcut.WorkingDirectory = $installDir
    $shortcut.IconLocation = Join-Path $installDir $link.Icon
    $shortcut.Save()
}
Write-Host "Build ready. Now run install.ps1 as administrator."
