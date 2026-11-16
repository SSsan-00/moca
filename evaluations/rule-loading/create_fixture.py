"""Explicitly authored mandatory/detail version of the same context-integrity case."""
from contextlib import redirect_stdout
from pathlib import Path
import io
import json
import runpy
import subprocess

# Reuse the same PHP data, checker and deliberate repair fault; only arrange the rule files.
with redirect_stdout(io.StringIO()):
    baseline = runpy.run_path(str(Path(__file__).parents[1] / 'context-integrity/create_fixture.py'))
root, app = baseline['root'], baseline['app']
background = []
for number in range(1, 5):
    path = app / f'rules/R{number}.md'
    lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
    background.extend(line for line in lines if line.startswith('The original order'))
    required = [line for line in lines if not line.startswith(('The original order', 'Implement Migration.'))]
    required.append('Preserve the original order, case, punctuation and duplicate values. Never guess data.\n')
    path.write_text(''.join(required), encoding='utf-8')
    if number == 1:
        (app / 'rules/order-values.md').write_text(''.join(line for line in lines if line.startswith('Implement Migration.')), encoding='utf-8')
(app / 'rules/background.md').write_text(''.join(background), encoding='utf-8')
# An unrelated detail document helps reveal unnecessary eager loading in Junie's tool log.
(app / 'rules/sql-example.md').write_text('SQL-only example, not applicable to this array migration.\n' * 200, encoding='utf-8')
config = (root / 'harness.toml').read_text(encoding='utf-8')
for path, description in (
    ('rules/order-values.md', 'OrderPage.Valuesを移行・修正するときの値の変換仕様。該当するときは必ず読む。'),
    ('rules/background.md', '原文の値・順序を保持する理由の補足説明。必須事項は各Rファイルに記載済み。'),
    ('rules/sql-example.md', 'SQLを含む移行の説明と例。配列のみの移行では不要。'),
):
    config += '\n[[project.migration_rules]]\npath=' + json.dumps(path) + '\nusage="reference"\ndescription=' + json.dumps(description, ensure_ascii=False) + '\n'
(root / 'harness.toml').write_text(config, encoding='utf-8')
subprocess.run(['git', '-C', str(app), 'add', 'rules'], check=True)
subprocess.run(['git', '-C', str(app), 'commit', '-qm', 'Separate authored instructions from detailed examples'], check=True)
print(root)
