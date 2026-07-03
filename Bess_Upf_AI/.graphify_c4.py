import sys, json
from pathlib import Path

# Empty semantic
Path('graphify-out/.graphify_semantic.json').write_text(json.dumps({'nodes':[],'edges':[],'hyperedges':[],'input_tokens':0,'output_tokens':0}), encoding='utf-8')

# Merge
ast = json.loads(Path('graphify-out/.graphify_ast.json').read_text(encoding='utf-8'))
sem = json.loads(Path('graphify-out/.graphify_semantic.json').read_text(encoding='utf-8'))

seen = {n['id'] for n in ast['nodes']}
merged_nodes = list(ast['nodes'])
for n in sem['nodes']:
    if n['id'] not in seen:
        merged_nodes.append(n)
        seen.add(n['id'])

merged_edges = ast['edges'] + sem['edges']
merged_hyperedges = sem.get('hyperedges', [])
merged = {
    'nodes': merged_nodes,
    'edges': merged_edges,
    'hyperedges': merged_hyperedges,
    'input_tokens': sem.get('input_tokens', 0),
    'output_tokens': sem.get('output_tokens', 0),
}
Path('graphify-out/.graphify_extract.json').write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding='utf-8')
print("Merged: {} nodes, {} edges".format(len(merged_nodes), len(merged_edges)))

# Step 4
from graphify.build import build_from_json
from graphify.cluster import cluster, score_all
from graphify.analyze import god_nodes, surprising_connections, suggest_questions
from graphify.report import generate
from graphify.export import to_json

extraction = json.loads(Path('graphify-out/.graphify_extract.json').read_text(encoding='utf-8'))
detection  = json.loads(Path('graphify-out/.graphify_detect.json').read_text(encoding='utf-8'))

G = build_from_json(extraction, root='.', directed=False)
if G.number_of_nodes() == 0:
    print('ERROR: Graph is empty - extraction produced no nodes.')
    raise SystemExit(1)
communities = cluster(G)
cohesion = score_all(G, communities)
tokens = {'input': extraction.get('input_tokens', 0), 'output': extraction.get('output_tokens', 0)}
gods = god_nodes(G)
surprises = surprising_connections(G, communities)
labels = {cid: 'Community ' + str(cid) for cid in communities}
questions = suggest_questions(G, communities, labels)

wrote = to_json(G, communities, 'graphify-out/graph.json')
if not wrote:
    print('ERROR: refused to shrink graphify-out/graph.json')
    raise SystemExit(1)
report = generate(G, communities, cohesion, labels, gods, surprises, detection, tokens, '.', suggested_questions=questions)
Path('graphify-out/GRAPH_REPORT.md').write_text(report, encoding='utf-8')
analysis = {
    'communities': {str(k): v for k, v in communities.items()},
    'cohesion': {str(k): v for k, v in cohesion.items()},
    'gods': gods,
    'surprises': surprises,
    'questions': questions,
}
Path('graphify-out/.graphify_analysis.json').write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding='utf-8')
print("Graph: {} nodes, {} edges, {} communities".format(G.number_of_nodes(), G.number_of_edges(), len(communities)))

# Step 4.5 - Health check
from graphify.diagnostics import diagnose_extraction, format_diagnostic_report
summary = diagnose_extraction(extraction, directed=False, root='.')
print(format_diagnostic_report(summary))
flags = []
if summary.get('dangling_endpoint_edges', 0): flags.append(f"{summary['dangling_endpoint_edges']} dangling-endpoint edges")
if summary.get('missing_endpoint_edges', 0): flags.append(f"{summary['missing_endpoint_edges']} missing-endpoint edges")
if summary.get('self_loop_edges', 0): flags.append(f"{summary['self_loop_edges']} self-loop edges")
if summary.get('directed_same_endpoint_collapsed_edges', 0): flags.append(f"{summary['directed_same_endpoint_collapsed_edges']} collapsed (directed) edges")
if summary.get('undirected_same_endpoint_collapsed_edges', 0): flags.append(f"{summary['undirected_same_endpoint_collapsed_edges']} collapsed (undirected) edges")

if flags:
    print('GRAPH HEALTH WARNING: ' + '; '.join(flags) + ' - graph may be incomplete/corrupt.')
else:
    print('Graph health: OK (no dangling/missing/collapsed edges).')

