.PHONY: test lint check install-dev

test:
	pytest tests/ -v --tb=short

lint:
	ruff check .
	vulture . vulture_whitelist.py --min-confidence 80

check: lint test

install-dev:
	pip install -r requirements-dev.txt
