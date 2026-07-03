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
        if 'vendor' not in nid and 'node_modules' not in nid and 'tests' not in nid:
            isolated.append(nid)

print(f"Total internal isolated nodes: {len(isolated)}")
for nid in isolated[:30]:
    print(f"- {nid}")
