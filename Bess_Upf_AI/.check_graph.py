import json
from pathlib import Path

graph = json.loads(Path('graphify-out/graph.json').read_text(encoding='utf-8'))
print("Node example:", graph['nodes'][0])
if graph['edges']:
    print("Edge example:", graph['edges'][0])
else:
    print("NO EDGES in graph.json!")
