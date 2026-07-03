#!/bin/sh
python scrape_to_kafka.py &
python -m bytewax.run pipeline.app:flow
