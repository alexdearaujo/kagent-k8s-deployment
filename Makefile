# Validation entry point shared by CI and the pre-push hook, so local and
# remote checks cannot drift apart.

.DEFAULT_GOAL := help
.PHONY: help validate lint format test docs plan secrets install-hooks

help: ## Show available targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

validate: lint test docs plan ## Run every check (what CI runs)

lint: ## Static analysis
	uv run ruff check src/ tests/

format: ## Apply formatting (not yet enforced; src/ predates ruff format)
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

test: ## Contract tests, including the rendered Helm manifest
	uv run pytest tests/ -q

docs: ## Markdown lint
	npx --yes markdownlint-cli2

plan: ## Both deploy scripts must produce a plan from the example configs
	KENTIK_COMPANY_ID=000000 uv run deploy-talos --dry-run > /dev/null
	KENTIK_COMPANY_ID=000000 uv run deploy-kagent \
		--config kagent.yaml.example --dry-run > /dev/null
	@echo "  dry runs OK"

secrets: ## Scan history for committed credentials (needs gitleaks)
	@command -v gitleaks >/dev/null 2>&1 \
		|| { echo "gitleaks not installed: brew install gitleaks"; exit 1; }
	gitleaks detect --redact --no-banner

install-hooks: ## Run validate automatically before every push
	@printf '#!/bin/sh\nexec make validate\n' > .git/hooks/pre-push
	@chmod +x .git/hooks/pre-push
	@echo "  pre-push hook installed"
