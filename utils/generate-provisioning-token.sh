#!/bin/bash

# Script to create a Kentik provisioning token for kagent Helm chart deployment
# Usage: ./generate-provisioning-token.sh --name <token-name> [options]
#
# Kentik API (flags override environment variables):
#   --api-root <host>               API host (or K_API_ROOT env var; default: grpc.api.kentik.com)
#   --api-email <email>             Kentik account email (or K_API_EMAIL env var)
#   --api-token <token>             Kentik API token (or K_API_TOKEN env var)
#
# Token Configuration:
#   --name <name>                   Required. User-friendly name for the token.
#   --max-usage <count>             Max agents that can use this token (default: 1)
#   --expires-at <ISO-8601>         Token expiration time (default: API default)
#   --auto-approve                  Skip manual approval (default: requires approval)
#   --site-id <id>                  Site ID to assign to registered agents

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
TOKEN_NAME=""
MAX_USAGE_COUNT=""
EXPIRES_AT=""
REQUIRES_APPROVAL="true"
SITE_ID=""

# ============================================================================
# Functions
# ============================================================================

usage() {
    cat <<EOF
Usage: $0 --name <token-name> [options]

Create a Kentik provisioning token for kagent Helm chart deployment.

Kentik API (flags override environment variables):
  --api-root <host>               API host (or K_API_ROOT env var; default: grpc.api.kentik.com)
  --api-email <email>             Kentik account email (or K_API_EMAIL env var)
  --api-token <token>             Kentik API token (or K_API_TOKEN env var)

Token Configuration:
  --name <name>                   Required. User-friendly name for the token.
  --max-usage <count>             Max agents that can use this token (default: 1)
                                  NOTE: must match the number of replicas you intend to deploy
  --expires-at <ISO-8601>         Token expiration time (default: API default)
  --auto-approve                  Skip manual approval (default: requires approval)
  --site-id <id>                  Site ID to assign to registered agents

Examples:
  # Minimal (uses env vars for auth):
  export K_API_EMAIL=user@company.com
  export K_API_TOKEN=abc123
  $0 --name "production-agents"

  # Full options with CLI auth:
  $0 --api-root grpc.api.kentik.eu \\
     --api-email user@co.com --api-token abc123 \\
     --name "staging-fleet" \\
     --max-usage 10
EOF
    exit "${1:-0}"
}

die() {
    printf 'Error: %b\n' "$1" >&2
    exit 1
}

check_curl() {
    if ! command -v curl &>/dev/null; then
        die "curl is required but not found. Please install curl."
    fi
}

check_jq() {
    if ! command -v jq &>/dev/null; then
        die "jq is required but not found. Please install jq."
    fi
}


build_request_body() {
    local body
    body=$(jq -n --arg name "$TOKEN_NAME" '{name: $name}')

    if [[ -n "$MAX_USAGE_COUNT" ]]; then
        body=$(echo "$body" | jq --argjson v "$MAX_USAGE_COUNT" '. + {maxUsageCount: $v}')
    fi

    if [[ -n "$EXPIRES_AT" ]]; then
        body=$(echo "$body" | jq --arg v "$EXPIRES_AT" '. + {expiresAt: $v}')
    fi

    if [[ "$REQUIRES_APPROVAL" == "true" ]]; then
        body=$(echo "$body" | jq '. + {requiresApproval: true}')
    else
        body=$(echo "$body" | jq '. + {requiresApproval: false}')
    fi

    if [[ -n "$SITE_ID" ]]; then
        body=$(echo "$body" | jq --arg v "$SITE_ID" '. + {config: {siteId: $v}}')
    fi

    echo "$body"
}

do_post() {
    local url="$1"
    local body="$2"

    curl -sS -w "\n%{http_code}" \
        -X POST \
        -H "Content-Type: application/json" \
        -H "X-CH-Auth-Email: $K_API_EMAIL" \
        -H "X-CH-Auth-API-Token: $K_API_TOKEN" \
        -d "$body" \
        "$url"
}

# ============================================================================
# Parse Arguments
# ============================================================================

while [[ $# -gt 0 ]]; do
    if [[ "$1" =~ ^--(api-email|api-token|name|max-usage|expires-at|site-id|api-root)$ ]]; then
        if [[ $# -lt 2 || "${2:-}" == --* ]]; then
            die "Missing value for $1"
        fi
    fi
    case "$1" in
        --api-email)
            K_API_EMAIL="$2"
            shift 2
            ;;
        --api-token)
            K_API_TOKEN="$2"
            shift 2
            ;;
        --name)
            TOKEN_NAME="$2"
            shift 2
            ;;
        --max-usage)
            MAX_USAGE_COUNT="$2"
            shift 2
            ;;
        --expires-at)
            EXPIRES_AT="$2"
            shift 2
            ;;
        --auto-approve)
            REQUIRES_APPROVAL="false"
            shift
            ;;
        --site-id)
            SITE_ID="$2"
            shift 2
            ;;
        --api-root)
            API_ROOT="$2"
            shift 2
            ;;
        --help|-h)
            usage 0
            ;;
        *)
            die "Unknown option: $1. Use --help for usage."
            ;;
    esac
done

# ============================================================================
# Validate Inputs
# ============================================================================

check_curl
check_jq

[[ -z "$K_API_EMAIL" ]] && die "Kentik email is required. Use --api-email or set K_API_EMAIL env var."
[[ -z "$K_API_TOKEN" ]] && die "Kentik API token is required. Use --api-token or set K_API_TOKEN env var."
[[ -z "$TOKEN_NAME" ]] && die "Token name is required. Use --name <name>."

if [[ -n "$MAX_USAGE_COUNT" ]]; then
    if ! [[ "$MAX_USAGE_COUNT" =~ ^[0-9]+$ ]] || [[ "$MAX_USAGE_COUNT" -lt 1 ]]; then
        die "--max-usage must be a positive integer."
    fi
fi

# ============================================================================
# Create Token
# ============================================================================

API_ROOT="${API_ROOT#https://}"
API_ROOT="${API_ROOT#http://}"
API_ROOT="${API_ROOT%/}"
URL="https://${API_ROOT}/kagent/v202401/provisioning-tokens"
BODY=$(build_request_body)

echo "Creating provisioning token..."
echo "  API:    $API_ROOT"
echo "  Name:   $TOKEN_NAME"
echo ""

RESPONSE=$(do_post "$URL" "$BODY") || die "curl request failed. Check network connectivity and API host: $URL"
HTTP_CODE=$(echo "$RESPONSE" | tail -1)
RESPONSE_BODY=$(echo "$RESPONSE" | sed '$d')

if [[ -z "$HTTP_CODE" || "$HTTP_CODE" -ne 200 ]]; then
    echo "API request failed (HTTP $HTTP_CODE):" >&2
    if echo "$RESPONSE_BODY" | jq . >/dev/null 2>&1; then
        echo "$RESPONSE_BODY" | jq . >&2
    else
        echo "$RESPONSE_BODY" >&2
    fi
    exit 1
fi

# Extract token value
PROV_TOKEN=$(echo "$RESPONSE_BODY" | jq -r '.token.token')
TOKEN_EXPIRES=$(echo "$RESPONSE_BODY" | jq -r '.token.expiresAt // "N/A"')
TOKEN_MAX_USAGE=$(echo "$RESPONSE_BODY" | jq -r '.token.maxUsageCount // 1')

if [[ -z "$PROV_TOKEN" || "$PROV_TOKEN" == "null" ]]; then
    die "Failed to extract token from API response. Full response:\n$RESPONSE_BODY"
fi

# ============================================================================
# Output
# ============================================================================

echo "✓ Provisioning token created successfully!"
echo ""
echo "  Token:       $PROV_TOKEN"
echo "  Expires:     $TOKEN_EXPIRES"
echo "  Max Agents:  $TOKEN_MAX_USAGE"
echo ""
echo "Use with Helm:"
echo ""
echo "  helm install kagent . \\"
echo "    --set-string kagent.companyId=YOUR_COMPANY_ID \\"
echo "    --set-string kagent.provisioningToken='$PROV_TOKEN'"
echo ""
