#!/bin/bash

# List provisioning tokens for the organization, or get details for one token.
# Usage: ./list-provisioning-tokens.sh [--id <token-id>] [options]
#
# Arguments:
#   --id <token-id>      Show details for a single token by ID
#
# Kentik API (flags override environment variables):
#   --api-root <host>    API host (or K_API_ROOT env var; default: grpc.api.kentik.com)
#   --api-email <email>  Kentik account email (or K_API_EMAIL env var)
#   --api-token <token>  Kentik API token (or K_API_TOKEN env var)

set -euo pipefail

# ============================================================================
# Load .env if present
# ============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/.env"
fi

# ============================================================================
# Defaults
# ============================================================================
K_API_EMAIL="${K_API_EMAIL:-}"
K_API_TOKEN="${K_API_TOKEN:-}"
API_ROOT="${K_API_ROOT:-grpc.api.kentik.com}"
TOKEN_ID=""

# ============================================================================
# Parse arguments
# ============================================================================
while [[ $# -gt 0 ]]; do
    case "$1" in
        --id)        TOKEN_ID="$2";    shift 2 ;;
        --api-email) K_API_EMAIL="$2"; shift 2 ;;
        --api-token) K_API_TOKEN="$2"; shift 2 ;;
        --api-root)  API_ROOT="$2";    shift 2 ;;
        --help|-h)
            sed -n '2,15p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown option: $1. Use --help for usage." >&2; exit 1 ;;
    esac
done

# ============================================================================
# Validate
# ============================================================================
[[ -z "$K_API_EMAIL" ]] && { echo "Error: K_API_EMAIL is required. Use --api-email or set K_API_EMAIL." >&2; exit 1; }
[[ -z "$K_API_TOKEN" ]] && { echo "Error: K_API_TOKEN is required. Use --api-token or set K_API_TOKEN." >&2; exit 1; }

command -v curl >/dev/null 2>&1 || { echo "Error: curl is required but not found." >&2; exit 1; }
command -v jq   >/dev/null 2>&1 || { echo "Error: jq is required but not found." >&2; exit 1; }

# ============================================================================
# Fetch tokens
# ============================================================================
API_ROOT="${API_ROOT#https://}"
API_ROOT="${API_ROOT#http://}"
API_ROOT="${API_ROOT%/}"

if [[ -n "$TOKEN_ID" ]]; then
    URL="https://${API_ROOT}/kagent/v202401/provisioning-tokens/${TOKEN_ID}"
    echo "Fetching token '${TOKEN_ID}'..."
else
    URL="https://${API_ROOT}/kagent/v202401/provisioning-tokens"
    echo "Fetching provisioning tokens..."
fi
echo "  API: $API_ROOT"
echo ""

RESPONSE=$(curl -sS -w "\n%{http_code}" \
    -X GET \
    -H "Content-Type: application/json" \
    -H "X-CH-Auth-Email: $K_API_EMAIL" \
    -H "X-CH-Auth-API-Token: $K_API_TOKEN" \
    "$URL")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')

if [[ "$HTTP_CODE" -ne 200 ]]; then
    echo "API request failed (HTTP $HTTP_CODE):" >&2
    echo "$BODY" | jq . 2>/dev/null || echo "$BODY" >&2
    exit 1
fi

if [[ -n "$TOKEN_ID" ]]; then
    # Single token response: { "token": { ... } }
    echo "$BODY" | jq -r '
        .token | [
            "  ID:                " + (.id                        // "N/A"),
            "  Name:              " + (.name                      // "N/A"),
            "  Token:             " + (.token                     // "N/A"),
            "  Max usage:         " + ((.maxUsageCount | tostring) // "unlimited"),
            "  Requires approval: " + (.requiresApproval | tostring),
            "  Expires:           " + (.expiresAt                 // "never"),
            "  Created:           " + (.createdAt                 // "N/A")
        ] | .[]'
else
    TOKEN_COUNT=$(echo "$BODY" | jq '.tokens | length')
    if [[ "$TOKEN_COUNT" -eq 0 ]]; then
        echo "No provisioning tokens found for this organization."
        exit 0
    fi
    echo "Found $TOKEN_COUNT token(s):"
    echo ""
    echo "$BODY" | jq -r '
        .tokens[] | [
            "  ID:                " + (.id                        // "N/A"),
            "  Name:              " + (.name                      // "N/A"),
            "  Token:             " + (.token                     // "N/A"),
            "  Max usage:         " + ((.maxUsageCount | tostring) // "unlimited"),
            "  Requires approval: " + (.requiresApproval | tostring),
            "  Expires:           " + (.expiresAt                 // "never"),
            "  Created:           " + (.createdAt                 // "N/A"),
            "  ---"
        ] | .[]'
fi
