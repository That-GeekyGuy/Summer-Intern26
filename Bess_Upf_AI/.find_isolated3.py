import json
from pathlib import Path

ast = json.loads(Path('graphify-out/.graphify_ast.json').read_text(encoding='utf-8'))
nodes = ast.get('nodes', [])
edges = ast.get('edges', [])

connected = set()
for e in edges:
    connected.add(e.get('source'))
    connected.add(e.get('target'))

isolated = []
for n in nodes:
    nid = n.get('id')
    if nid not in connected:
        src = n.get('metadata', {}).get('src', '')
        if 'vendor' not in src and 'node_modules' not in src and 'tests' not in src:
            isolated.append((nid, src))

for nid, src in isolated[:30]:
    print(f"- {nid} (src: {src})")
