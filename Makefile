PYTHON ?= python3
.PHONY: test demo check
test:
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v
demo:
	PYTHONPATH=src $(PYTHON) examples/smoke.py
	PYTHONPATH=src $(PYTHON) examples/sample_plan.py
check: test demo
