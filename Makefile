.PHONY: run inspect profiles gtfs
run:
	python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8000
inspect:
	python3 data_pipeline/inspect_data.py --data-dir data --output data_report.json
profiles:
	python3 data_pipeline/build_profiles.py --archive data/raw/October-Delhi.csv.zip --output data/processed/crowd_profiles.json
gtfs:
	python3 data_pipeline/build_gtfs_index.py --archive data/raw/delhi_buses_static_gtfs_v1.zip --output data/processed/gtfs.sqlite
