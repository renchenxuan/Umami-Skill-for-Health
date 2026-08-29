#requires -Version 5
$ErrorActionPreference = "Stop"
# NOTE: ASCII-only messages to avoid PS 5.1 console codepage (GBK) mojibake.
$src = Join-Path $PSScriptRoot "skills\umami-health"
if (-not (Test-Path (Join-Path $src "SKILL.md"))) { throw "SKILL.md not found under $src" }
$dst = Join-Path $HOME ".agents\skills\umami-health"
New-Item -ItemType Directory -Force -Path (Split-Path $dst -Parent) | Out-Null
if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
Copy-Item -Recurse -Force $src $dst
Write-Host "Installed to $dst"
Write-Host "Restart ZCode / Codex to discover skill: umami-health"
