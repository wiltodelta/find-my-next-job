#!/usr/bin/env bash

set -euo pipefail

uv sync
uv run uv-outdated
uv run uv-secure --ignore-unfixed
uv run ruff check --fix
uv run ruff format
uv run pyright
