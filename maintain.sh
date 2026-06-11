#!/usr/bin/env bash

set -euo pipefail

uv sync
# uvx, not `uv run`: uv-outdated is not a project dep; uvx runs it in its own
# isolated env.
uvx uv-outdated
uv run uv-secure
uv run ruff check --fix
uv run ruff format
uv run pyright
