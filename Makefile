SHELL := /bin/bash
.ONESHELL:

PYTHON ?= python3
VENV := .venv
PIP := $(VENV)/bin/pip
FLASK := $(VENV)/bin/flask
PORT ?= 5000

.PHONY: help run setup venv install env check-poppler

help:
	@echo "make run    - set up environment and run the app"
	@echo "make setup  - create venv, install deps, and prepare .env"

venv:
	@echo "Creating virtual environment $(VENV)"
	@$(PYTHON) -m venv $(VENV)

install: venv
	@echo "Installing requirements..."
	@$(PIP) install -r requirements.txt

env:
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "Created .env from .env.example"; \
	fi

check-poppler:
	@if ! command -v pdftoppm >/dev/null 2>&1; then \
		echo "Poppler not found. Install with:"; \
		echo "  macOS: brew install poppler"; \
		echo "  Linux: sudo apt-get install -y poppler-utils"; \
	fi

setup: env install check-poppler

run: setup
	@echo "Starting SAGE at http://127.0.0.1:$(PORT)"
	@$(FLASK) --app app run --debug --port $(PORT)
