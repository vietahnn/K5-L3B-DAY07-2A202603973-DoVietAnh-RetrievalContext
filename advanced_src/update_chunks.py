import json, re

with open('advanced_data/advanced_chunks.json', 'r', encoding='utf-8') as f:
    chunks = json.load(f)

for c in chunks:
    if c['type'] == 'table':
        m = re.search(r'TABLE\s+(\d+)', c['caption'], re.IGNORECASE)
        if m:
            t_num = m.group(1)
            c['table_num'] = int(t_num)
            c['text'] = f"[TABLE {t_num} - Page {c['page']}]\n" + c['text']
    elif c['type'] == 'figure':
        m = re.search(r'fig(?:ure)?\.?\s*(\d+)', c['caption'], re.IGNORECASE)
        if m:
            f_num = m.group(1)
            c['figure_num'] = int(f_num)
            c['text'] = f"[FIGURE {f_num} - Page {c['page']}]\n" + c['text']

with open('advanced_data/advanced_chunks.json', 'w', encoding='utf-8') as f:
    json.dump(chunks, f, ensure_ascii=False, indent=2)

print('Updated chunks successfully!')
