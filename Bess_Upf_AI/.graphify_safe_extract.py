import sys, json
from graphify.extract import collect_files, extract
from pathlib import Path
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, TimeoutError

detect = json.loads(Path('graphify-out/.graphify_detect.json').read_text(encoding='utf-8'))
code_files = []
for f in detect.get('files', {}).get('code', []):
    code_files.extend(collect_files(Path(f)) if Path(f).is_dir() else [Path(f)])

bad_files = {
    'models/moment_channel_names.json', 'models/moment_threshold.json', 'models/scaler_params.json', 'models/split_report.json', 'models/stl_profiles.json',
    'pipeline/app.py', 'pipeline/scrape_to_kafka.py', 'pipeline/start.sh', 'pipeline/tests/__init__.py', 'pipeline/tests/test_scraper_schema.py',
    'pipeline/tests/test_window_state.py', 'pipeline/window.py', 'run.ps1', 'run.sh', 'scripts/fetch-models.sh', 'scripts/gen-dev-certs.sh',
    'scripts/minio-init.sh', 'scripts/start.ps1', 'scripts/start.sh', 'serve/app.py', 'tools/infer/chronos_server.py', 'tools/infer/moment_server.py',
    'tools/infer/server.py', 'tools/temporal/calendar_context.py', 'tools/temporal/correlation_analysis.py', 'tools/temporal/regime_classifier.py',
    'tools/temporal/stl_service.py', 'tools/train/ablation.py', 'tools/train/data/profile_report.json', 'tools/train/data/synthetic_profile.json',
    'tools/train/generate_synthetic_data.py', 'tools/train/prepare_dataset.py', 'tools/train/tests/test_prepare.py', 'tools/train/train.py',
    'tools/train/train_chronos.py', 'tools/train/train_moment.py', 'upf-sim/go.mod', 'upf-sim/internal/api/handler.go', 'upf-sim/internal/sim/engine.go',
    'upf-sim/internal/sim/engine_test.go', 'upf-sim/internal/sim/sequence.go', 'upf-sim/main.go',
}

safe_code_files = []
for f in code_files:
    f_str = str(f).replace('\\\\', '/')
    if not any(f_str.endswith(bad) for bad in bad_files):
        safe_code_files.append(f)

print(f"Skipped {len(code_files) - len(safe_code_files)} known bad files. Safe to parse: {len(safe_code_files)}")

def parse_file(f):
    try:
        return extract([f], cache_root=Path('.'), parallel=False)
    except Exception:
        return None

if __name__ == '__main__':
    results_nodes = []
    results_edges = []
    results_input = 0
    results_output = 0

    if safe_code_files:
        with ProcessPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(parse_file, f): f for f in safe_code_files}
            for i, fut in enumerate(futures):
                try:
                    res = fut.result(timeout=10)
                    if res:
                        results_nodes.extend(res.get('nodes', []))
                        results_edges.extend(res.get('edges', []))
                        results_input += res.get('input_tokens', 0)
                        results_output += res.get('output_tokens', 0)
                except TimeoutError:
                    print(f"TIMEOUT: {futures[fut]}")
                except Exception as e:
                    print(f"ERROR: {futures[fut]} - {e}")
                if (i+1) % 100 == 0:
                    print(f"Extracted {i+1}/{len(safe_code_files)} files...")

    ast_result = {'nodes': results_nodes, 'edges': results_edges, 'input_tokens': results_input, 'output_tokens': results_output}
    Path('graphify-out/.graphify_ast.json').write_text(json.dumps(ast_result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f"AST extraction complete: {len(results_nodes)} nodes, {len(results_edges)} edges.")
