.PHONY: test spec build

test:
	uv run python -m unittest discover -s tests -p 'test_*.py'

# Refresh the public API spec copy used by tests/test_spec_coverage.py.
spec:
	curl -fsS https://api.notamify.com/api/v2/openapi.json | python3 -m json.tool > tests/fixtures/openapi.json

# Build notamify-sdk and the notamify CLI distribution into dist/.
build:
	uv build --out-dir dist .
	uv build --no-sources --out-dir dist cli
