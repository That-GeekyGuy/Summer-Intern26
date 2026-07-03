import json
from pathlib import Path
d = json.loads(Path('graphify-out/.graphify_detect.json').read_text(encoding='utf-8'))
files = d.get('total_files', 0)
words = d.get('total_words', 0)
print(f'Corpus: {files} files, ~{words} words')
for k, v in d.get('files', {}).items():
    if len(v) > 0:
        print(f'  {k}: {len(v)} files')

skipped = d.get('skipped_sensitive', [])
if skipped:
    print(f'Skipped {len(skipped)} sensitive files')

if words > 2000000 or files > 500:
    print('WARNING_TRIGGERED')
    scan_root = d.get('scan_root', '')
    from collections import Counter
    counts = Counter()
    for cat, flist in d.get('files', {}).items():
        for f in flist:
            if scan_root + '/graphify-out/' in f or scan_root + '\\\\graphify-out\\\\' in f:
                continue
            try:
                rel = Path(f).relative_to(Path(scan_root))
                parts = rel.parts
                if len(parts) > 1:
                    counts[parts[0]] += 1
                else:
                    counts['(root)'] += 1
            except ValueError:
                pass
    for k, v in counts.most_common(5):
        print(f'  {k}: {v}')
