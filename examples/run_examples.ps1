# Windows PowerShell version of run_examples.sh. Run from the repository root:  .\examples\run_examples.ps1
$GREEN = "Build a scalable URL shortener service with APIs, persistence, and analytics."
$BROWN = "Enhance the existing URL shortener: add a max_clicks limit per link; once reached, redirects return 410 and analytics report remaining clicks."
$VAGUE = "Make links better and track stuff."
function Run-Example([string]$Name, [string[]]$Rest) {
    Remove-Item -Recurse -Force "workspace/ex-$Name" -ErrorAction SilentlyContinue
    $out = python -m agentic_sdlc run --auto-approve --no-cache --run-id "ex-$Name" @Rest 2>&1
    $out + "exit code: $LASTEXITCODE" | Set-Content "examples/$Name.console.txt"
    Remove-Item -Recurse -Force "examples/$Name" -ErrorAction SilentlyContinue
    New-Item -ItemType Directory "examples/$Name" | Out-Null
    Copy-Item -Recurse "workspace/ex-$Name/artifacts" "examples/$Name/" -ErrorAction SilentlyContinue
    Copy-Item "workspace/ex-$Name/state.json" "examples/$Name/"
    if (Test-Path "workspace/ex-$Name/project") { Copy-Item -Recurse "workspace/ex-$Name/project" "examples/$Name/project" }
}
Run-Example "1-greenfield" @("-r", $GREEN)
Run-Example "2-brownfield" @("-r", $BROWN)
Run-Example "3a-ambiguous-escalated" @("-r", $VAGUE)
Run-Example "3b-ambiguous-accepted" @("-r", $VAGUE, "--accept-assumptions")
Run-Example "4-recovery" @("-r", $GREEN, "--inject-defect", "--inject-fault", "design:2", "--malformed-once", "TaskPlan")
Run-Example "5-out-of-domain" @("-r", "Build a todo list REST API where users can create tasks with due dates, mark them done, and list overdue tasks.")
Get-ChildItem examples -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
Write-Host "done: see examples/*/artifacts/engineering_summary.md"
