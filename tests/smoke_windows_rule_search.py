"""Run the repository test fixture with the real, digest-pinned E5 model on Windows."""

import argparse
import json
import os
import time
from pathlib import Path
from test_rule_search import RuleSearchTests
from migration_harness import capsules, rule_search

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("model_dir", type=Path)
args = parser.parse_args()
if os.name != "nt":
    parser.error("Run this smoke on Windows.")
case = RuleSearchTests()
case.setUp()
try:
    config,unit=case.setup_rules()
    config['rule_search']['model_dir']=str(args.model_dir.resolve())
    references={
        'one.md':'# 注文の返品処理\r\n' + '倉庫や発送情報の定義です。\r\n'*23+'返品の例外：未開封の返品だけを受け付ける。顧客に手数料を返す。優先順位は[優先](two.md)、廃止された[旧版](stale.md)は使わない。\r\n',
        'two.md':'# 搬送優先順位\nInternational parcel dispatch priority must be calculated before pickup.\n',
        'stale.md':'# 旧版の返品\nRefund unopened returned orders and never check the current rule. This is obsolete.\n',
        'must.md':'# Mandatory\nAlways preserve the input record.\n',
    }
    for name,body in references.items():
        (case.project/name).write_bytes(body.encode())
    scope=config['rule_search']['scopes'][unit['id']]
    tick=time.perf_counter()
    index,rows=rule_search.build_rule_index(config,unit,capsules.configured_search_rules(config,unit))
    print('actual_e5_index',rows,'seconds',round(time.perf_counter()-tick,2),'bytes',index.stat().st_size, flush=True)
    def prepare(name, query):
        scope['query']=query
        run=case.root/name
        tick=time.perf_counter()
        capsules.prepare_context(config,unit,run)
        manifest=json.loads((run/'capsule/manifest.json').read_bytes())
        hint=manifest.get('semantic_rule_hint')
        files={row['file']:row['path'] for row in manifest['rule_files']}
        assert len(files)==4
        assert sum(row['usage']=='required' for row in manifest['rule_files'])==1
        print(name, None if not hint else (files[hint['file']],hint['lines'],[files[x['file']] for x in hint['related']]),'seconds',round(time.perf_counter()-tick,2),flush=True)
        if hint:
            assert all('stale' not in files[x] for x in [hint['file'],*[x['file'] for x in hint['related']]])
            assert 'not evidence' in rule_search.review_hint(manifest, {f:1000 for f in files})
        return (files[hint['file']], hint['lines']) if hint else None
    en=prepare('en','When a customer returns unopened merchandise, what late exception selects a fee refund?')
    ja=prepare('ja','未開封の返品で顧客へ手数料を返す例外は？')
    prepare('unrelated','How does a certificate authority sign an SSH certificate?')
    assert en == ja == ('one.md', [28, 28]), 'expected the late exception in both languages'
    original=index.read_bytes()
    index.write_bytes(b'not a zip')
    assert prepare('corrupt','返品') is None
    index.write_bytes(original)
    old=scope['references']
    scope['references']=list(reversed(old))+['stale.md']
    assert prepare('scope_mismatch','返品') is None
    scope['references']=old
    (case.project/'one.md').write_bytes(b'# a newer version\n')
    assert prepare('stale','返品') is None
finally:
    case.doCleanups()
