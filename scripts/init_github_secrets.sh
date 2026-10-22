#!/usr/bin/env bash
# Provision PSS account secrets from local environment variables.
#
# Credentials are NEVER hardcoded here. Set, per account i (1..5):
#   PSS_ACCOUNT_i_EMAIL
#   PSS_ACCOUNT_i_PASSWORD
#   PSS_ACCOUNT_i_REFRESH_TOKEN
# before running. Usage: ./init_github_secrets.sh
set -euo pipefail

set_secret() {
    local name="$1" value="${2:-}"
    if [ -z "$value" ]; then
        echo "ERROR: missing $name input (export it before running)" >&2
        exit 1
    fi
    gh secret set "$name" -b "$value"
}

for i in 1 2 3 4 5; do
    email_var="PSS_ACCOUNT_${i}_EMAIL"
    password_var="PSS_ACCOUNT_${i}_PASSWORD"
    refresh_var="PSS_ACCOUNT_${i}_REFRESH_TOKEN"
    set_secret "PSS_ACCOUNT_${i}_EMAIL" "${!email_var}"
    set_secret "PSS_ACCOUNT_${i}_PASSWORD" "${!password_var}"
    set_secret "PSS_ACCOUNT_${i}_REFRESH_TOKEN" "${!refresh_var}"
done
