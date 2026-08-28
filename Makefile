PY=.venv/bin/python
.PHONY: data load api
data:      ## trim raw CSVs -> data/
	python3 scripts/prepare_data.py
load:      ## create schema + load data into ClickHouse (per .env)
	$(PY) scripts/load.py
api:       ## run FastAPI on :8800
	cd backend && ../$(PY) -m uvicorn main:app --host 0.0.0.0 --port 8800 --reload
