import sys, json
from pathlib import Path
from graphify.detect import detect
from graphify.extract import collect_files, extract
from graphify.build import build_from_json
from graphify.cluster import cluster, score_all
from graphify.analyze import god_nodes, surprising_connections, suggest_questions
from graphify.report import generate
from graphify.export import to_json

print("1. Running detection...")
detect_result = detect(Path('.'))
Path('graphify-out/.graphify_detect.json').write_text(json.dumps(detect_result, ensure_ascii=False), encoding='utf-8')

print("2. Extracting AST with parallel=False (safe mode)...")
code_files = []
for f in detect_result.get('files', {}).get('code', []):
    code_files.extend(collect_files(Path(f)) if Path(f).is_dir() else [Path(f)])

if code_files:
    ast_result = extract(code_files, cache_root=Path('.'), parallel=False)
else:
    ast_result = {'nodes':[],'edges':[],'input_tokens':0,'output_tokens':0}
Path('graphify-out/.graphify_ast.json').write_text(json.dumps(ast_result, indent=2, ensure_ascii=False), encoding='utf-8')

print("3. Generating semantic (empty)...")
sem_result = {'nodes':[],'edges':[],'hyperedges':[],'input_tokens':0,'output_tokens':0}
Path('graphify-out/.graphify_semantic.json').write_text(json.dumps(sem_result), encoding='utf-8')

print("4. Merging...")
seen = {n['id'] for n in ast_result['nodes']}
merged_nodes = list(ast_result['nodes'])
for n in sem_result['nodes']:
    if n['id'] not in seen:
        merged_nodes.append(n)
        seen.add(n['id'])

merged_edges = ast_result['edges'] + sem_result['edges']
merged_result = {
    'nodes': merged_nodes,
    'edges': merged_edges,
    'hyperedges': sem_result.get('hyperedges', []),
    'input_tokens': sem_result.get('input_tokens', 0),
    'output_tokens': sem_result.get('output_tokens', 0),
}
Path('graphify-out/.graphify_extract.json').write_text(json.dumps(merged_result, indent=2, ensure_ascii=False), encoding='utf-8')

print("5. Building graph...")
G = build_from_json(merged_result, root='.', directed=False)
if G.number_of_nodes() == 0:
    print('ERROR: Graph is empty')
    sys.exit(1)
communities = cluster(G)
cohesion = score_all(G, communities)
gods = god_nodes(G)
surprises = surprising_connections(G, communities)

labels = {}
for cid, nodes in communities.items():
    if len(nodes) > 0:
        labels[cid] = "Comm " + nodes[0].split('/')[-1].split('\\\\')[-1][:20]
    else:
        labels[cid] = "Community " + str(cid)

questions = suggest_questions(G, communities, labels)

to_json(G, communities, 'graphify-out/graph.json')
tokens = {'input': 0, 'output': 0}
report = generate(G, communities, cohesion, labels, gods, surprises, detect_result, tokens, '.', suggested_questions=questions)
Path('graphify-out/GRAPH_REPORT.md').write_text(report, encoding='utf-8')

print("Graph rebuilt with {} nodes and {} edges!".format(G.number_of_nodes(), G.number_of_edges()))
