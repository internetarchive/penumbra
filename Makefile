.PHONY: install
install:
	uv sync
	uv run playwright install --with-deps chromium

.PHONY: format
format:
	uv run ruff check --select I --fix
	uv run ruff format

.PHONY: lint
lint:
	uv run ruff check

.PHONY: run
run:
	uv run python src/penumbra/worker.py

.PHONY: test
test:
	uv run pytest .
