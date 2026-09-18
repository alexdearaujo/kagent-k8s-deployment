# Validation entry point shared by CI and the pre-push hook, so local and
# remote checks cannot drift apart.

.DEFAULT_GOAL := help
.PHONY: help validate lint format test docs plan privacy pre-commit secrets \
	install-hooks

help: ## Show available targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

validate: lint test docs plan privacy ## Run every check (what CI runs)

lint: ## Static analysis
	uv run ruff check src/ tests/ tools/

format: ## Apply formatting (not yet enforced; src/ predates ruff format)
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

test: ## Contract tests, including the rendered Helm manifest
	uv run pytest tests/ -q

docs: ## Markdown lint
	npx --yes markdownlint-cli2

# The loaders read credentials from the environment. CI has no .env, so supply
# dummies: a dry run never contacts Proxmox or Kentik.
DUMMY_ENV := KENTIK_COMPANY_ID=test PROXMOX_USER=test \
	PROXMOX_TOKEN_NAME=test PROXMOX_TOKEN_SECRET=test

plan: ## Both deploy scripts must produce a plan from the example configs
	$(DUMMY_ENV) uv run deploy-talos --config talos.yaml.example --dry-run > /dev/null
	$(DUMMY_ENV) uv run deploy-kagent --config kagent.yaml.example --dry-run > /dev/null
	@echo "  dry runs OK"

privacy: ## Scan every tracked file for private information
	@uv run python tools/privacy_scan.py --all

# What the pre-commit hook runs. Staged content only, so it stays fast enough
# to sit in front of every commit. gitleaks is the credential half and is
# optional locally because CI runs it on every push regardless.
pre-commit: ## Scan the staged changes (privacy scan plus gitleaks)
	@uv run python tools/privacy_scan.py
	@if command -v gitleaks >/dev/null 2>&1; then \
		gitleaks git --staged --redact --no-banner; \
	else \
		echo "  gitleaks not installed, skipping secret scan: brew install gitleaks"; \
	fi

secrets: ## Scan history for committed credentials (needs gitleaks)
	@command -v gitleaks >/dev/null 2>&1 \
		|| { echo "gitleaks not installed: brew install gitleaks"; exit 1; }
	gitleaks git --redact --no-banner

install-hooks: ## Install the pre-commit and pre-push hooks
	@printf '#!/bin/sh\nexec make pre-commit\n' > .git/hooks/pre-commit
	@printf '#!/bin/sh\nexec make validate\n' > .git/hooks/pre-push
	@chmod +x .git/hooks/pre-commit .git/hooks/pre-push
	@echo "  pre-commit and pre-push hooks installed"
