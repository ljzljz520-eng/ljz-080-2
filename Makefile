.PHONY: install seed run sim test reset docs

install:
	python3 -m pip install -r requirements.txt

seed:
	python3 -m app.seed

reset:
	rm -f data/island_energy.db data/island_energy.db-wal data/island_energy.db-shm
	python3 -m app.seed

run:
	ENERGY_SIMULATOR_AUTOSTART=1 python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

sim:
	python3 -m app.simulator

test:
	python3 -m pytest tests/ -q
