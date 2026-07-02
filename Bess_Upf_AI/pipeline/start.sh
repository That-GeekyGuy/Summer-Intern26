#!/bin/sh
python scrape_to_kafka.py &
python app.py
