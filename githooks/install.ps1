# Enable the versioned git hooks of this repository and create the version file.
# Run once per clone, in a regular PowerShell window in the repository folder.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

git config core.hooksPath githooks
if ($LASTEXITCODE -ne 0) { throw "git config failed." }

# Run the hook once, so corridoor/_version.py exists before the first commit.
$sh = Join-Path (Split-Path (Split-Path (Get-Command git).Source)) "usr\bin\sh.exe"
if (-not (Test-Path $sh)) { $sh = "sh" }
& $sh githooks/post-commit
if ($LASTEXITCODE -ne 0) { throw "The post-commit hook failed." }
Write-Host ("Hooks enabled. " + (Get-Content corridoor/_version.py | Select-String "__version__"))
