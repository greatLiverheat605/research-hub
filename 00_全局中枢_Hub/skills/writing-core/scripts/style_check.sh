#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   bash style_check.sh <markdown_file>

FILE="${1:-}"
if [[ -z "$FILE" ]]; then
  echo "Usage: bash style_check.sh <markdown_file>"
  exit 1
fi
if [[ ! -f "$FILE" ]]; then
  echo "File not found: $FILE"
  exit 1
fi

search_re() {
  local mode="$1"
  local pattern="$2"
  local target="$3"
  if command -v rg >/dev/null 2>&1; then
    if [[ "$mode" == "ci" ]]; then
      rg -ni "$pattern" "$target" || echo "OK"
    else
      rg -n "$pattern" "$target" || echo "OK"
    fi
  else
    if [[ "$mode" == "ci" ]]; then
      grep -Eni "$pattern" "$target" || echo "OK"
    else
      grep -En "$pattern" "$target" || echo "OK"
    fi
  fi
}

echo "Checking: $FILE"

printf "\n[1] Unnecessary bold markers (** or __):\n"
search_re "cs" '\*\*|__' "$FILE"

printf "\n[2] List lines (正文需谨慎使用):\n"
search_re "cs" '^[[:space:]]*([-*+][[:space:]]+|[0-9]+\.[[:space:]]+)' "$FILE"

printf "\n[3] Banned transition/emphasis phrases:\n"
search_re "cs" '首先|其次|最后|此外|另外|接下来|值得注意的是|需要指出的是|重要的是|必须强调的是' "$FILE"

printf "\n[4] Banned English transition/emphasis phrases:\n"
search_re "ci" 'first and foremost|to begin with|moreover|furthermore|last but not least|it is worth noting that|importantly' "$FILE"

printf "\n[5] Subjective first-person phrases (论文正文慎用):\n"
search_re "ci" '我认为|我觉得|我个人(认为|看法)|我的研究|in my opinion|i believe' "$FILE"

printf "\n[6] Consecutive non-empty lines (可能缺少段间空行，人工复核):\n"
awk '
  {
    if (NR>1) {
      prev=lines[NR-1]
      curr=$0
      if (prev !~ /^[[:space:]]*$/ && curr !~ /^[[:space:]]*$/ && prev !~ /^[[:space:]]*```/ && curr !~ /^[[:space:]]*```/ && prev !~ /^[[:space:]]*[#>|-]/ && curr !~ /^[[:space:]]*[#>|-]/ && prev !~ /^[[:space:]]*[0-9]+\./ && curr !~ /^[[:space:]]*[0-9]+\./) {
        print NR-1 ":" prev
        print NR ":" curr
        print "---"
      }
    }
    lines[NR]=$0
  }
' "$FILE" || true

printf "\nDone.\n"
