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
        # Only look at internal code files
        if 'vendor' not in src and 'node_modules' not in src and 'tests' not in src:
            isolated.append((nid, src))

print(f"Total internal isolated nodes: {len(isolated)}")
from collections import defaultdict
by_file = defaultdict(list)
for nid, src in isolated:
    if src:
        by_file[src].append(nid)

print("\nFiles with most isolated components:")
for src, nids in sorted(by_file.items(), key=lambda x: len(x[1]), reverse=True)[:10]:
    print(f"- {src} ({len(nids)} isolated nodes): {nids[:3]}...")

