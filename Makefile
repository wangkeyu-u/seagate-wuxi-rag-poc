.PHONY: quality generate validate test evaluate full-test check run

quality:
	python3 -m compileall -q server.py rag_app scripts tests
	node --check static/app.js

generate:
	python3 scripts/generate_data.py

validate:
	python3 scripts/validate_data.py

test:
	python3 -m unittest discover -s tests -v

evaluate:
	python3 scripts/evaluate.py

full-test:
	python3 scripts/full_system_test.py

check: quality generate validate test evaluate full-test

run:
	python3 server.py --host 127.0.0.1 --port 8787 --dev-auth
