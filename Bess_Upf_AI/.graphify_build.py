import sys, json
from pathlib import Path
from graphify.build import build_from_json
from graphify.cluster import cluster, score_all
from graphify.analyze import god_nodes, surprising_connections, suggest_questions
from graphify.report import generate
from graphify.export import to_json

ast = json.loads(Path('graphify-out/.graphify_ast.json').read_text(encoding='utf-8'))

manual_edges = [
    ('frontend/src/api/client.ts', 'frontend/src/features/anomalies/RCAPanel.tsx'),
    ('frontend/src/api/client.ts', 'frontend/src/features/anomalies/AnomalyFeedPage.tsx'),
    ('frontend/src/api/client.ts', 'frontend/src/features/overview/OverviewPage.tsx'),
    ('frontend/src/api/client.ts', 'frontend/src/features/forecast/ForecastPage.tsx'),
    ('frontend/src/api/client.ts', 'frontend/src/components/layout/ContextPanel.tsx'),
    ('frontend/src/api/client.ts', 'detection/internal/detector/rules.go'),
    ('frontend/src/api/client.ts', 'detection/internal/detector/trend.go'),
    ('frontend/src/api/client.ts', 'detection/internal/detector/threshold.go'),
    ('frontend/src/api/client.ts', 'detection/internal/detector/zscore.go'),
    ('frontend/src/api/client.ts', 'analysis/internal/llm/orchestrator.go'),
    ('frontend/src/api/client.ts', 'analysis/internal/detector/client.go'),
    ('frontend/src/api/client.ts', 'analysis/internal/api/handler.go'),
    ('frontend/src/api/client.ts', 'detection/internal/store/store.go'),
    ('frontend/src/api/client.ts', 'detection/internal/tier2/aieval.go'),
    ('frontend/src/api/client.ts', 'detection/internal/tier2/client.go'),
    ('frontend/src/App.tsx', 'frontend/src/main.tsx'),
    ('frontend/src/App.tsx', 'frontend/e2e/shell.spec.ts'),
]

for src, tgt in manual_edges:
    ast['edges'].append({
        'source': src,
        'target': tgt,
        'type': 'references',
        'metadata': {'weight': 1, 'injected': True}
    })

print("Building graph...")
G = build_from_json(ast, root='.', directed=False)
if G.number_of_nodes() == 0:
    print('ERROR: Graph is empty')
    sys.exit(1)

print("Clustering...")
communities = cluster(G)
print("Scoring cohesion...")
cohesion = score_all(G, communities)
print("Finding god nodes...")
gods = god_nodes(G)
print("Finding surprises...")
surprises = surprising_connections(G, communities)

labels = {}
for cid, nodes in communities.items():
    if len(nodes) > 0:
        labels[cid] = "Comm " + nodes[0].split('/')[-1].split('\\\\')[-1][:20]
    else:
        labels[cid] = "Community " + str(cid)

print("Suggesting questions...")
questions = suggest_questions(G, communities, labels)

print("Exporting to graph.json...")
to_json(G, communities, 'graphify-out/graph.json')

detect_result = json.loads(Path('graphify-out/.graphify_detect.json').read_text(encoding='utf-8'))
tokens = {'input': 0, 'output': 0}
report = generate(G, communities, cohesion, labels, gods, surprises, detect_result, tokens, '.', suggested_questions=questions)
Path('graphify-out/GRAPH_REPORT.md').write_text(report, encoding='utf-8')

print("Graph rebuilt with {} nodes and {} edges!".format(G.number_of_nodes(), G.number_of_edges()))
