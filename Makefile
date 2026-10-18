# Contingency Atlas Workbench — everyday commands.
.DEFAULT_GOAL := help

PY := .venv/bin/python
ATLAS := .venv/bin/atlas

.PHONY: help start install demo serve run status doctor test lint check clean

help:  ## Show this help
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

start:  ## Set up everything and open the dashboard (same as ./start.sh)
	./start.sh

install:  ## Create the virtualenv and install the package
	ATLAS_INSTALL_ONLY=1 ./start.sh

demo:  ## Load the fictional demo corpus (no dashboard)
	$(ATLAS) demo

serve:  ## Open the dashboard
	$(ATLAS) serve

run:  ## Run the deterministic analysis over the current data
	$(ATLAS) run

status:  ## Show library and review counts
	$(ATLAS) status

doctor:  ## Check the installation
	$(ATLAS) doctor

test:  ## Run the test suite
	$(PY) -m pytest -q

lint:  ## Run the CI lint gate
	$(PY) -m ruff check --select F821,F811,F402,E9,F841 contingency_atlas scripts tests

check: lint test  ## Lint and test

clean:  ## Delete local data (asks for confirmation)
	$(ATLAS) clean
