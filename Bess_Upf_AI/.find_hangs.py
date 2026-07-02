import sys, json, time
from graphify.extract import collect_files, extract
from pathlib import Path

code_files = []
detect = json.loads(Path('graphify-out/.graphify_detect.json').read_text(encoding='utf-8'))
for f in detect.get('files', {}).get('code', []):
    code_files.extend(collect_files(Path(f)) if Path(f).is_dir() else [Path(f)])

import multiprocessing
from concurrent.futures import ProcessPoolExecutor, TimeoutError

def parse_file(f):
    try:
        return extract([f], cache_root=Path('.'), parallel=False)
    except Exception as e:
        return None

results = []
print(f"Testing {len(code_files)} files for hanging...")
with ProcessPoolExecutor(max_workers=4) as executor:
    futures = {executor.submit(parse_file, f): f for f in code_files}
    for i, fut in enumerate(futures):
        try:
            res = fut.result(timeout=10)
            if res:
                results.append(res)
        except TimeoutError:
            print(f"HANG DETECTED: {futures[fut]}")
        except Exception as e:
            print(f"ERROR: {futures[fut]} - {e}")
        
        if i % 100 == 0:
            print(f"Processed {i}/{len(code_files)}")

