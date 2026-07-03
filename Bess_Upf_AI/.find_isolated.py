import json
from pathlib import Path

graph = json.loads(Path('graphify-out/graph.json').read_text(encoding='utf-8'))
edges = graph.get('edges', [])
connected = set()
for e in edges:
    connected.add(e.get('source'))
    connected.add(e.get('target'))

isolated = []
for n in graph.get('nodes', []):
    nid = n.get('id')
    if nid not in connected:
        src = n.get('metadata', {}).get('src', '')
        if 'vendor' not in src:
            isolated.append((nid, src))

print(f"Total non-vendor isolated nodes: {len(isolated)}")
for nid, src in isolated[:20]:
    print(f"- {nid} ({src})")
