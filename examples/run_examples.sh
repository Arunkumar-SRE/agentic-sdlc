#!/usr/bin/env bash
# Regenerates every example into examples/<name>/ (artifacts + console output). Run from the repository root.
set -u
GREEN="Build a scalable URL shortener service with APIs, persistence, and analytics."
BROWN="Enhance the existing URL shortener: add a max_clicks limit per link; once reached, redirects return 410 and analytics report remaining clicks."
VAGUE="Make links better and track stuff."
run() {  # name, args...
  name=$1; shift
  rm -rf "workspace/ex-$name"
  python -m agentic_sdlc run --auto-approve --no-cache --run-id "ex-$name" "$@" > "examples/$name.console.txt" 2>&1
  echo "exit code: $?" >> "examples/$name.console.txt"
  rm -rf "examples/$name" && mkdir -p "examples/$name"
  cp -r "workspace/ex-$name/artifacts" "examples/$name/" 2>/dev/null
  cp "workspace/ex-$name/state.json" "examples/$name/"
  [ -d "workspace/ex-$name/project" ] && cp -r "workspace/ex-$name/project" "examples/$name/project"
}
run 1-greenfield             -r "$GREEN"
run 2-brownfield             -r "$BROWN"
run 3a-ambiguous-escalated   -r "$VAGUE"
run 3b-ambiguous-accepted    -r "$VAGUE" --accept-assumptions
run 4-recovery               -r "$GREEN" --inject-defect --inject-fault design:2 --malformed-once TaskPlan
run 5-out-of-domain          -r "Build a todo list REST API where users can create tasks with due dates, mark them done, and list overdue tasks."
find examples -name __pycache__ -type d -prune -exec rm -rf {} + ; find examples -name ".pytest-report.xml" -delete
echo "done: see examples/*/artifacts/engineering_summary.md"
