#!/usr/bin/env bash
# Port of Shasta c45741109495652c809a1da326eeeccf334d0310:
# ci/templates/code-review-cursor.yml. Review policy/limits are unchanged.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CURSOR_REVIEW_PROMPT_TEMPLATE="$(cat "$SCRIPT_DIR/cursor-review-prompt.txt")"
# Cursor CLI Code Review for GitHub Pull Requests (ported from Shasta)
set -e

echo "=========================================="
echo "Cursor AI Code Review"
echo "=========================================="
echo ""

# Check if cursor CLI is available
if ! command -v cursor-agent &> /dev/null; then
    echo "❌ Cursor CLI not found in PATH"
    echo "   Expected: cursor-agent command"
    echo "   Please ensure cursor-agent is installed in the Docker image"
    echo "   Visit: https://cursor.com/docs/cli"
    exit 1
fi

echo "✅ Cursor CLI found: $(cursor-agent --version 2>&1 | head -1)"
echo ""

# Check for API key
if [ -z "$CURSOR_API_KEY" ]; then
    echo "❌ CURSOR_API_KEY environment variable not set"
    echo "   Please configure CURSOR_API_KEY in GitHub Actions Secrets"
    exit 1
fi

echo "✅ API Key configured"
export CURSOR_API_KEY
echo ""

# Get MR information
MR_SOURCE="${PR_SOURCE:-HEAD}"
MR_TARGET="${PR_TARGET:-main}"
MR_IID="${PR_NUMBER:-N/A}"

echo "Pull Request Info:"
echo "  PR number: $MR_IID"
echo "  Source: $MR_SOURCE"
echo "  Target: $MR_TARGET"
echo ""

# GitHub token supplied by the workflow; it is not a model credential.
TOKEN="${GITHUB_TOKEN:-}"
TOKEN_HEADER="Authorization"
TOKEN_VALUE="Bearer $TOKEN"
if [ -n "$TOKEN" ]; then
    echo "Using GITHUB_TOKEN for GitHub API"
else
    echo "⚠️  No GitHub token available, skip history-comment check"
fi
echo ""

# MR-only job: require workflow-provided base SHA and commit SHA
if [ -z "${PR_DIFF_BASE_SHA:-}" ]; then
    echo "❌ PR_DIFF_BASE_SHA is missing (this job is MR-only)"
    exit 1
fi
if [ -z "${PR_HEAD_SHA:-}" ]; then
    echo "❌ PR_HEAD_SHA is missing"
    exit 1
fi

DIFF_BASE="$PR_DIFF_BASE_SHA"
DIFF_HEAD="$PR_HEAD_SHA"

# Fetch the MR diff base commit (shallow clone may not have it)
git fetch origin "$DIFF_BASE" --depth=1 2>/dev/null || \
    git fetch origin --unshallow 2>/dev/null || \
    git fetch origin --depth=100 2>/dev/null || true

# Two-dot diff is reliable in shallow clone (does not require merge-base)
DIFF_RANGE="$DIFF_BASE..$DIFF_HEAD"

# Create output directory and patch-id tracking files
mkdir -p cursor_review_results
REVIEW_OUTPUT="cursor_review_results/review_report.md"
REVIEWED_PATCH_FILE="cursor_review_results/reviewed_patch_ids.txt"
CURRENT_PATCH_FILE="cursor_review_results/current_patch_commit_map.tsv"
NEW_PATCH_FILE="cursor_review_results/new_patch_ids.txt"
NEW_COMMIT_FILE="cursor_review_results/new_commits.txt"
: > "$REVIEWED_PATCH_FILE"
: > "$CURRENT_PATCH_FILE"
: > "$NEW_PATCH_FILE"
: > "$NEW_COMMIT_FILE"

# Read reviewed patch-ids from MR comments (hidden marker)
if [ -n "$GITHUB_API_URL" ] && [ "$MR_IID" != "N/A" ] && [ -n "$TOKEN" ]; then
    echo "Loading reviewed patch-ids from MR comments..."
    NOTES_API_URL="$GITHUB_API_URL/repos/$GITHUB_REPOSITORY/issues/$MR_IID/comments?per_page=100"
    if MR_NOTES_JSON=$(curl --silent --show-error --fail \
        --header "$TOKEN_HEADER: $TOKEN_VALUE" \
        "$NOTES_API_URL" 2>/dev/null); then
        echo "$MR_NOTES_JSON" | jq -r '.[].body // ""' | \
            sed -n 's/.*<!-- cursor-reviewed-patchids:\([0-9a-fA-F, ]*\) -->.*/\1/p' | \
            tr ',' '\n' | tr -d ' ' | sed '/^$/d' | tr 'A-F' 'a-f' | sort -u > "$REVIEWED_PATCH_FILE"
        echo "Loaded reviewed patch-ids: $(wc -l < "$REVIEWED_PATCH_FILE")"
    else
        echo "⚠️  Failed to query MR notes, continue without patch-id history."
    fi
    echo ""
fi

# Build current patch-id -> commit map
CURRENT_COMMITS=$(git rev-list --reverse --no-merges "$DIFF_RANGE" 2>/dev/null || echo "")
while IFS= read -r commit; do
    [ -z "$commit" ] && continue
    PATCH_ID=$(git show --pretty=format: "$commit" | git patch-id --stable 2>/dev/null | awk 'NR==1 {print $1}')
    if [ -n "$PATCH_ID" ]; then
        PATCH_ID=$(echo "$PATCH_ID" | tr 'A-F' 'a-f')
        echo "$PATCH_ID	$commit" >> "$CURRENT_PATCH_FILE"
    fi
done <<< "$CURRENT_COMMITS"

# Keep unique patch-id -> first commit mapping
awk -F '\t' '!seen[$1]++ {print $1 "\t" $2}' "$CURRENT_PATCH_FILE" > "${CURRENT_PATCH_FILE}.uniq"
mv "${CURRENT_PATCH_FILE}.uniq" "$CURRENT_PATCH_FILE"

# Select new patch-ids and related commits
while IFS=$'\t' read -r patch_id commit; do
    [ -z "$patch_id" ] && continue
    if ! grep -qx "$patch_id" "$REVIEWED_PATCH_FILE"; then
        echo "$patch_id" >> "$NEW_PATCH_FILE"
        echo "$commit" >> "$NEW_COMMIT_FILE"
    fi
done < "$CURRENT_PATCH_FILE"

if [ -s "$NEW_PATCH_FILE" ]; then
    sort -u "$NEW_PATCH_FILE" -o "$NEW_PATCH_FILE"
fi
if [ -s "$NEW_COMMIT_FILE" ]; then
    sort -u "$NEW_COMMIT_FILE" -o "$NEW_COMMIT_FILE"
fi

NEW_PATCH_COUNT=$(wc -l < "$NEW_PATCH_FILE")
echo "New patch-ids to review: $NEW_PATCH_COUNT"

if [ "$NEW_PATCH_COUNT" -eq 0 ]; then
    echo "All patch-ids already reviewed. Skipping AI review."
    echo "[]" > cursor_review_results/code-quality-report.json
    echo "Review skipped: all patch-ids already reviewed." > "$REVIEW_OUTPUT"
    exit 0
fi

# Get changed files only from new commits
CHANGED_FILES=$(
    while IFS= read -r commit; do
        [ -z "$commit" ] && continue
        git diff-tree --no-commit-id --name-only -r "$commit" 2>/dev/null || true
    done < "$NEW_COMMIT_FILE" | sort -u
)

echo "Changed Files:"
echo "$CHANGED_FILES"
echo ""

# Filter for C/C++ code files only
CODE_FILES=$(echo "$CHANGED_FILES" | grep -E '\.(cpp|c|h|hpp|cc|cxx|hxx)$' || echo "")

if [ -z "$CODE_FILES" ]; then
    echo "No code files changed. Skipping AI review."
    # Create empty artifacts to avoid GitLab CI upload errors
    mkdir -p cursor_review_results
    echo "[]" > cursor_review_results/code-quality-report.json
    echo "No code files changed. Skipping AI review." > cursor_review_results/review_report.md
    exit 0
fi

# Per-MR budget: review at most N code files
MAX_REVIEW_FILES="${MAX_REVIEW_FILES:-15}"
CODE_FILES_TOTAL=$(echo "$CODE_FILES" | sed '/^$/d' | wc -l)
if [ "$CODE_FILES_TOTAL" -gt "$MAX_REVIEW_FILES" ]; then
    echo "Code files exceed budget ($CODE_FILES_TOTAL > $MAX_REVIEW_FILES), limiting review scope."
    CODE_FILES=$(echo "$CODE_FILES" | sed '/^$/d' | head -n "$MAX_REVIEW_FILES")
else
    CODE_FILES=$(echo "$CODE_FILES" | sed '/^$/d')
fi

echo "Code files to review:"
echo "$CODE_FILES"
echo ""

# Initialize review report
cat > "$REVIEW_OUTPUT" << 'EOREPORT'
# 🤖 Cursor AI Code Review Report

## Pull Request Summary

EOREPORT

echo "- **MR**: #$MR_IID" >> "$REVIEW_OUTPUT"
echo "- **Files Changed**: $(echo "$CHANGED_FILES" | sed '/^$/d' | wc -l)" >> "$REVIEW_OUTPUT"
echo "- **Code Files Detected**: $CODE_FILES_TOTAL" >> "$REVIEW_OUTPUT"
echo "- **Code Files Reviewed**: $(echo "$CODE_FILES" | wc -l)" >> "$REVIEW_OUTPUT"
echo "- **New Patch IDs Reviewed**: $NEW_PATCH_COUNT" >> "$REVIEW_OUTPUT"
echo "" >> "$REVIEW_OUTPUT"
echo "---" >> "$REVIEW_OUTPUT"
echo "" >> "$REVIEW_OUTPUT"

# Review each changed file
echo "Running AI code review..."
echo ""

# Collect files with no bugs to reduce report noise (printed at end)
NO_BUG_LIST_FILE=$(mktemp)

FILE_COUNT=0
while IFS= read -r file; do
    if [ -z "$file" ] || [ ! -f "$file" ]; then
        continue
    fi

    FILE_COUNT=$((FILE_COUNT + 1))
    echo "[$FILE_COUNT] Reviewing: $file"

    # Create temporary diff file first to avoid storing large diffs in shell variables
    TEMP_DIFF=$(mktemp)
    while IFS= read -r commit; do
        [ -z "$commit" ] && continue
        git show -W -U20 --pretty=format: "$commit" -- "$file" >> "$TEMP_DIFF" 2>/dev/null || true
    done < "$NEW_COMMIT_FILE"

    # Skip if there is no diff content for this file
    if [ ! -s "$TEMP_DIFF" ]; then
        rm -f "$TEMP_DIFF"
        continue
    fi

    # Check diff size and prepare content with truncation if needed
    MAX_DIFF_LINES=1000
    DIFF_LINES=$(wc -l < "$TEMP_DIFF")
    echo "  - Patch lines for $file: $DIFF_LINES"
    if [ "$DIFF_LINES" -gt "$MAX_DIFF_LINES" ]; then
        DIFF_CONTENT=$(sed -n "1,${MAX_DIFF_LINES}p" "$TEMP_DIFF")
        DIFF_NOTE="(Note: diff truncated, showing first $MAX_DIFF_LINES of $DIFF_LINES lines)"
        echo "  - Truncated to $MAX_DIFF_LINES lines"
    else
        DIFF_CONTENT=$(cat "$TEMP_DIFF")
        DIFF_NOTE=""
    fi

# Create review prompt for Cursor AI (YAML variable + placeholder substitution)
if [ -z "${CURSOR_REVIEW_PROMPT_TEMPLATE:-}" ]; then
    echo "❌ CURSOR_REVIEW_PROMPT_TEMPLATE is not set"
    exit 1
fi

REVIEW_PROMPT="$CURSOR_REVIEW_PROMPT_TEMPLATE"
REVIEW_PROMPT="${REVIEW_PROMPT//__CURSOR_FILE__/$file}"
REVIEW_PROMPT="${REVIEW_PROMPT//__CURSOR_DIFF_NOTE__/$DIFF_NOTE}"
REVIEW_PROMPT="${REVIEW_PROMPT//__CURSOR_DIFF_CONTENT__/$DIFF_CONTENT}"

    # Call cursor-agent with the review prompt
    set +e
    CURSOR_RESPONSE=$(echo "$REVIEW_PROMPT" | CURSOR_API_KEY="$CURSOR_API_KEY" cursor-agent --force --model "$CURSOR_MODEL" -p 2>&1)
    CURSOR_EXIT=$?
    set -e

    # Normalize response (strip CR, trim, drop blank lines) for strict match
    CURSOR_RESPONSE_NORM=$(
        printf "%s\n" "$CURSOR_RESPONSE" \
          | tr -d '\r' \
          | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
          | sed '/^$/d'
    )

    # If no bugs found, only collect file name (printed at end)
    if [ "$CURSOR_EXIT" -eq 0 ] && [ "$CURSOR_RESPONSE_NORM" = "✅ No bugs found" ]; then
        printf "%s\n" "$file" >> "$NO_BUG_LIST_FILE"
    else
        # Otherwise, keep full per-file details at the top of the report
        echo "## 📄 \`$file\`" >> "$REVIEW_OUTPUT"
        echo "" >> "$REVIEW_OUTPUT"
        echo "### AI Review" >> "$REVIEW_OUTPUT"

        if [ "$CURSOR_EXIT" -ne 0 ]; then
            echo "⚠️ cursor-agent failed (exit $CURSOR_EXIT) for file: $file"
            echo "--- cursor-agent error output ---"
            echo "$CURSOR_RESPONSE"
            echo "---------------------------------"
            echo "⚠️ cursor-agent failed (exit $CURSOR_EXIT)" >> "$REVIEW_OUTPUT"
            echo "" >> "$REVIEW_OUTPUT"
        elif [ -n "$CURSOR_RESPONSE" ]; then
            echo "$CURSOR_RESPONSE" >> "$REVIEW_OUTPUT"
            echo "" >> "$REVIEW_OUTPUT"
        else
            echo "⚠️ No response from Cursor CLI." >> "$REVIEW_OUTPUT"
        fi

        echo "" >> "$REVIEW_OUTPUT"
        echo "---" >> "$REVIEW_OUTPUT"
        echo "" >> "$REVIEW_OUTPUT"
    fi

    # Clean up
    rm -f "$TEMP_DIFF"

done <<< "$CODE_FILES"

# Print aggregated list of no-bug files at the end (only when non-empty)
if [ -s "$NO_BUG_LIST_FILE" ]; then
    echo "## ✅ No bugs found" >> "$REVIEW_OUTPUT"
    echo "" >> "$REVIEW_OUTPUT"
    sort -u "$NO_BUG_LIST_FILE" | while IFS= read -r f; do
        [ -z "$f" ] && continue
        echo "- \`$f\`" >> "$REVIEW_OUTPUT"
    done
    echo "" >> "$REVIEW_OUTPUT"
fi
rm -f "$NO_BUG_LIST_FILE"


echo ""
echo "✅ AI Review Complete!"
echo ""
echo "Review report: $REVIEW_OUTPUT"
echo ""

# Display the report
cat "$REVIEW_OUTPUT"

# Generate Code Quality report for GitLab
echo ""
echo "Generating Code Quality report..."
CODE_QUALITY_FILE="cursor_review_results/code-quality-report.json"

cat > "$CODE_QUALITY_FILE" << 'EOJSON'
[
EOJSON

# Add entries for each reviewed file
FIRST_ENTRY=true
while IFS= read -r file; do
    if [ -f "$file" ]; then
        if [ "$FIRST_ENTRY" = false ]; then
            echo "," >> "$CODE_QUALITY_FILE"
        fi
        FIRST_ENTRY=false

        cat >> "$CODE_QUALITY_FILE" << EOENTRY
  {
    "description": "Cursor AI Code Review completed for this file. See full report in artifacts.",
    "check_name": "cursor_ai_review",
    "fingerprint": "$(echo -n "$file" | md5sum | cut -d' ' -f1)",
    "severity": "info",
    "location": {
      "path": "$file",
      "lines": {
        "begin": 1
      }
    }
  }
EOENTRY
    fi
done <<< "$CODE_FILES"

cat >> "$CODE_QUALITY_FILE" << 'EOJSON'
]
EOJSON

echo "✅ Code Quality report generated: $CODE_QUALITY_FILE"

# Post review to GitHub PR
if [ -n "$GITHUB_API_URL" ] && [ "$MR_IID" != "N/A" ]; then
    echo ""
    echo "Posting review to GitHub PR..."

    if [ -n "$TOKEN" ]; then
        # Persist reviewed patch-ids in hidden marker for next pipeline
        cat "$REVIEWED_PATCH_FILE" "$NEW_PATCH_FILE" 2>/dev/null | sed '/^$/d' | sort -u > cursor_review_results/all_reviewed_patch_ids.txt
        PATCH_IDS_MARKER=$(paste -sd, cursor_review_results/all_reviewed_patch_ids.txt)
        if [ -n "$PATCH_IDS_MARKER" ]; then
            echo "" >> "$REVIEW_OUTPUT"
            echo "<!-- cursor-reviewed-patchids:$PATCH_IDS_MARKER -->" >> "$REVIEW_OUTPUT"
        fi

        # Format review as MR comment
        COMMENT_BODY=$(cat "$REVIEW_OUTPUT" | jq -Rs .)

        HTTP_CODE=$(curl --silent --output /dev/null --write-out "%{http_code}" \
            --request POST \
            --header "$TOKEN_HEADER: $TOKEN_VALUE" \
            --header "Content-Type: application/json" \
            --data "{\"body\": $COMMENT_BODY}" \
            "$GITHUB_API_URL/repos/$GITHUB_REPOSITORY/issues/$MR_IID/comments")

        if [ "$HTTP_CODE" = "201" ]; then
            echo "✅ Posted review comment to MR #$MR_IID"
        else
            echo "⚠️  Failed to post to MR (HTTP $HTTP_CODE)"
            echo "   Check the workflow token's pull-requests: write permission."
        fi
    else
        echo "⚠️  No token available, skipping MR comment"
    fi
else
    echo ""
    echo "ℹ️  Not a merge request pipeline, skipping MR comment"
fi

echo ""
echo "=========================================="
echo "Review Complete"
echo "=========================================="
