#!/usr/bin/env bash
#
# Fails while this repo is still the unmodified ib.python-template scaffold, so
# that no placeholder image or Helm chart is ever published.
#
# Safe to delete once it has passed. See the `scaffold-guard` job in
# .github/workflows/ci.yml for what else to remove alongside it.
#
# Note: no `set -e`. Two of the three checks treat a non-zero grep as the
# success path, and the final arithmetic test is the intended exit status.
set -uo pipefail

TEMPLATE_SLUG="ib.python-template"

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

REPO_SLUG="${GITHUB_REPOSITORY:-}"
REPO_NAME="${REPO_SLUG##*/}"
REPO_NAME="${REPO_NAME:-<repo>}"

lines=()
fixes=()

pass() { lines+=("✓ $1"); }
fail() { lines+=("✗ $1 — $2"); }

if [[ -d helm/app ]]; then
  fail "helm/app/" "still the placeholder directory name"
  fixes+=("git mv helm/app helm/<service>")
else
  pass "helm/"
fi

if grep -qF "${TEMPLATE_SLUG}" sonar-project.properties 2>/dev/null; then
  fail "sonar-project.properties" "still references ${TEMPLATE_SLUG}"
  fixes+=("# in sonar-project.properties, set:")
  fixes+=("#   sonar.projectKey=Infoblox-CTO_${REPO_NAME}")
  fixes+=("#   sonar.projectName=${REPO_NAME}")
else
  pass "sonar-project.properties"
fi

if grep -qF "${TEMPLATE_SLUG}" README.md 2>/dev/null; then
  fail "README.md" "still describes the template, not your service"
  fixes+=("# rewrite README.md to describe your service")
else
  pass "README.md"
fi

render() {
  echo "### Scaffold customization check"
  echo
  printf '%s\n' "${lines[@]}"
  if (( ${#fixes[@]} > 0 )); then
    echo
    echo "This repo is still the ${TEMPLATE_SLUG} scaffold."
    echo "CI will not build or publish an image until it is customized."
    echo
    echo "Replace <service> with your service name (e.g. dhcp-lease-api), then:"
    echo
    printf '    %s\n' "${fixes[@]}"
    echo
    echo "Nothing else needs editing — the Makefile and CI workflow discover the"
    echo "helm/ chart directory automatically."
    echo
    echo "Renaming src/ is not required: unlike Go's cmd/<binary>, src/ is a"
    echo "standard Python layout name, not a placeholder. Replace the starter"
    echo "app in src/app.py with your service when you're ready."
  fi
}

render
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  render >> "$GITHUB_STEP_SUMMARY"
fi

(( ${#fixes[@]} == 0 ))
