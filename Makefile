APP_NAME    ?= $(notdir $(CURDIR))
# The scaffold ships helm/app, which gets renamed per service, so discover it
# instead of hardcoding. Override from the environment if a repo ever carries
# more than one chart.
CHART_DIR   ?= $(firstword $(wildcard helm/*))
# git describe only returns something semver-shaped once the repo has a tag;
# on an untagged repo it fails, so fall back to a valid semver prerelease
# built from the short commit hash (a bare hex hash like "b037143" is NOT
# valid semver on its own and helm package will reject it).
GIT_DESCRIBE := $(shell git describe --tags --dirty 2>/dev/null)
VERSION      ?= $(if $(GIT_DESCRIBE),$(patsubst v%,%,$(GIT_DESCRIBE)),0.0.0-$(shell git rev-parse --short HEAD))

.PHONY: all lint test build run ui show-image-version helm-package scaffold-check clean local-build local-push

all: lint test build

show-image-version:
	@echo $(VERSION)

lint:
	pip install -r requirements-dev.txt
	black --check .
	flake8 src tests

test:
	pip install -r requirements-dev.txt
	pytest --cov=src --cov-report=xml tests/

build:
	pip install -r requirements.txt

# Pipeline: poll Fusion, route, validate, close released orders. Serves /healthz.
run:
	python -m src.app

# Streamlit UI. `python -m` keeps the repo root on sys.path so `src.*` imports work.
ui:
	python -m streamlit run src/ui_layer/app.py

helm-package:
	helm package $(CHART_DIR) --version $(VERSION) --app-version $(VERSION)

# Same check CI runs; lets you clear the gate before pushing.
scaffold-check:
	@./.github/scripts/scaffold-guard.sh

clean:
	rm -rf .pytest_cache htmlcov *.xml *.tgz

# ──────────────────────────────────────────────────────────────
# Developer dev-push targets (not used by CI)
# Pushes locally built image to harbor.services.sdp.infoblox.com/infobloxcto-dev
# All developers have push access; selected dev clusters can pull.
# ──────────────────────────────────────────────────────────────
DEV_REGISTRY ?= harbor.services.sdp.infoblox.com/infobloxcto-dev
DEV_IMAGE    := $(DEV_REGISTRY)/$(APP_NAME)
DEV_TAG      ?= $(USER)-$(shell git rev-parse --short HEAD)

local-build:
	docker build -t $(DEV_IMAGE):$(DEV_TAG) .
	@echo "Built: $(DEV_IMAGE):$(DEV_TAG)"

local-push: local-build
	docker push $(DEV_IMAGE):$(DEV_TAG)
	@echo "Pushed: $(DEV_IMAGE):$(DEV_TAG)"
