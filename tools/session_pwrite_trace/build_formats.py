"""Bind the exact local tracepoint record ABI before compiling probes."""
import hashlib
import json
from pathlib import Path
import re
import sys

out = Path(sys.argv[1])
points = ['nvme/nvme_setup_cmd', 'nvme/nvme_complete_rq', 'nvme/nvme_sq',
          'ext4/ext4_ext_map_blocks_enter', 'ext4/ext4_ext_map_blocks_exit',
          'ext4/ext4_ext_convert_to_initialized_enter',
          'ext4/ext4_journal_start_inode', 'ext4/ext4_mark_inode_dirty',
          'jbd2/jbd2_handle_start', 'jbd2/jbd2_handle_stats',
          'jbd2/jbd2_start_commit', 'jbd2/jbd2_end_commit']
records, source = {}, []
for i, point in enumerate(points, 30):
    path = Path('/sys/kernel/tracing/events') / point / 'format'
    text = path.read_text()
    fields = [(int(a), int(b)) for a, b in
              re.findall(r'offset:(\d+);\s*size:(\d+);', text)]
    size = max(a+b for a, b in fields)-8
    assert size <= 96
    records[str(i)] = dict(point=point, bytes=size, format=text,
        sha256=hashlib.sha256(text.encode()).hexdigest())
    admin_filter = ''
    if i in (30, 31, 32):
        disk_offset = 12 if i == 32 else 8
        admin_filter = f'''  u64 disk_name = 0;
  if (read_checked(&disk_name, 8, (char*)c + {disk_offset})) return 0;
  if (!disk_name) return 0;
'''
    source.append(f'''SEC("tracepoint/{point}")
int normal_{i}(void* c) {{
  if (!enabled()) return 0;
{admin_filter}  u32 z = 0; u32* running = lookup(&normal_running, &z);
  if (running) *running = {i};
  struct event* e = start({i}, 0);
  if (!e) {{ if (running) *running = 0; return 0; }}
  read_checked(e->data, {size}, (char*)c + 8);
  submit(e, 0); if (running) *running = 0; return 0;
}}
''')
(out/'trace_formats.h').write_text('\n'.join(source))
(out/'formats.json').write_text(json.dumps(records, indent=2)+'\n')
