PYTHON ?= python
PYTEST ?= $(PYTHON) -m pytest -p no:anyio

.PHONY: install test test-fast test-slow clean

install:
	$(PYTHON) -m pip install -r requirements.txt

# Default: everything that doesn't require Node.js
test: test-fast

test-fast:
	$(PYTEST) -m "not slow"

# Full suite (needs Node.js + npm)
test-slow:
	$(PYTEST)

clean:
	rm -rf .pytest_cache tests/__pycache__ tests/*/__pycache__
	find . -name "*.pyc" -delete
