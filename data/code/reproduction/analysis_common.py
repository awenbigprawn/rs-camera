#!/usr/bin/env python3
"""Export paper metrics from accepted runs, without overwriting source data.

Use --archive for the original rs-camera/results tree or paper_used_raw_data.
Without --archive, --input is a reproduce.py output directory.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import sys

HERE=Path(__file__).resolve().parent

def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    obj=importlib.util.module_from_spec(spec); sys.modules[name]=obj
    spec.loader.exec_module(obj)
    return obj

def read(path): return json.loads(path.read_text())
def rows(path):
    with path.open(newline='') as stream: return list(csv.DictReader(stream))
def write(path,data):
    if not data: raise ValueError(f'No rows for {path}')
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='') as stream:
        w=csv.DictWriter(stream,fieldnames=list(data[0]),lineterminator='\n')
        w.writeheader(); w.writerows(data)
def one(paths):
    values=list(paths)
    if len(values)!=1: raise ValueError(f'Expected exactly one selected artifact, found {values}')
    return values[0]
def attempt(logical):
    return logical/('attempt-'+(logical/'selected_attempt.txt').read_text().strip())

def role(symbols,signature):
    for token,name in [('udev_device_watcher','udev watcher'),('time_diff_keeper','Time-difference keeper'),
                       ('thermal','Thermal monitor'),('polling_error_handler','Firmware-error poller'),
                       ('notifications_processor','Notification dispatcher')]:
        if token in symbols: return name
    if 'v4l_uvc_device9stream_on' in symbols:
        return 'Depth/IR capture' if 'multi_pins_uvc_device' in symbols else 'Color capture'
    if 'dispatcher' in symbols: return 'Pipeline dispatcher'
    if 'realsense_steady_probe' in signature: return 'Application wait'
    raise ValueError(f'Unclassified worker; inspect creator stack: {signature}')
