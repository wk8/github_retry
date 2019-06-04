SHELL := /usr/bin/env bash

activate: venv
	@ [ -f activate ] || (ln -s venv/bin/activate . && $(MAKE) requirements)

requirements: activate
	. activate && venv/bin/pip install -r requirements.txt

venv:
	@ [ -d venv ] || python3 -m venv venv

freeze: activate
	. activate && venv/bin/pip freeze > requirements.txt

TEST_COMMAND = pytest -vv --capture=no --cov=.

test: activate
	. activate && $(TEST_COMMAND)

cover: activate
	. activate && $(TEST_COMMAND) --cov-report html && open htmlcov/index.html

pep8: activate
	. activate && pycodestyle *.py --max-line-length=120
