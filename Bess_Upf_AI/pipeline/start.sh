#!/bin/sh
python pipeline/scrape_to_kafka.py &
python pipeline/forecast_check.py &
python -m bytewax.run pipeline.app:flow
