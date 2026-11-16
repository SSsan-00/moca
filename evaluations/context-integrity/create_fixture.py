from pathlib import Path
import tempfile, subprocess, json, sys

# All writes stay in a new temporary fixture; the real application is never edited.
root=Path(tempfile.mkdtemp(prefix='harness-rules-context-')).resolve()
app=root/'app'
for sub in ['Pages/Orders','Pages/inc','legacy','rules']:
    (app/sub).mkdir(parents=True,exist_ok=True)
def write(name,text): (app/name).write_text(text,encoding='utf-8')
write('Case.csproj','<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net9.0</TargetFramework><ImplicitUsings>enable</ImplicitUsings></PropertyGroup></Project>')
write('Program.cs','Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(Migration.OrderPage.Values()));\n')
write('Pages/Orders/OrderPage.cs','namespace Migration; public static class OrderPage { public static string[] Values() => []; }\n')
write('Pages/Orders/LocalLabels.cs','namespace Migration; public static class LocalLabels { public const string Prefix = "ORDER:"; }\n')
write('Pages/inc/SharedSuffix.cs','namespace Migration; public static class SharedSuffix { public const string Value = ":END"; }\n')
values=[f'order-{i:02d}-'+str(i*i+13) for i in range(24)]
write('legacy/order','<?php\n$values = [];\n'+''.join(f'$values[] = "{value}";\n' for value in values)+'echo json_encode($values);\n')
for i in range(4):
    name=f'R{i+1}'
    text=f'# Rule {name}\nAdd public const string Rule{i+1}Start = "{name}-START" to OrderPage.\n'
    text+='The original order, case, punctuation and duplicate values are significant. Do not guess data.\n'*40
    text+=f'Add public const string Rule{i+1}End = "{name}-END" to OrderPage.\n'
    if i==0: text+='Implement Migration.OrderPage.Values() returning string[]. For each PHP value prepend LocalLabels.Prefix and append SharedSuffix.Value. Use those existing constants, do not duplicate their literals. Inspect their sources in the target parent folder and sibling inc folder.\n'
    write(f'rules/{name}.md',text)
expected=['ORDER:'+value+':END' for value in values]
write('expected.json',json.dumps(expected))
write('check.py','''import json, subprocess, pathlib, sys
p=pathlib.Path('Pages/Orders/OrderPage.cs')
s=p.read_text()
errors=[]
for i in range(1,5):
    for suffix in ['Start','End']:
        if f'Rule{i}{suffix}' not in s or f'R{i}-{suffix.upper()}' not in s:
            errors.append(f'R{i} Pages/Orders/OrderPage.cs:1 warning: missing Rule{i}{suffix} constant required by common rules')
for helper in ['LocalLabels.Prefix','SharedSuffix.Value']:
    if helper not in s: errors.append(f'REF Pages/Orders/OrderPage.cs:1 warning: use {helper} from existing context')
actual=json.loads(subprocess.check_output(['dotnet','run','--no-build'],text=True))
if actual!=json.loads(pathlib.Path('expected.json').read_text()): errors.append('DATA Pages/Orders/OrderPage.cs:1 warning: output differs from the exact original ordered data and required prefix/suffix')
print('\\n'.join(errors) if errors else 'PASS: 24 ordered values, 8 rule markers, both adjacent helpers')
sys.exit(bool(errors))
''')
# Force one repair to verify that rules are read during repairs too.
write('break.py',"from pathlib import Path\np=Path('Pages/Orders/OrderPage.cs')\ns=p.read_text()\np.write_text(s.replace('R4-END','R4-BROKEN'))\n")
subprocess.run(['git','init','-q',str(app)],check=True)
for k,v in [('user.name','Harness validation'),('user.email','harness@example.invalid'),('commit.gpgsign','false')]: subprocess.run(['git','-C',str(app),'config',k,v],check=True)
subprocess.run(['git','-C',str(app),'add','.'],check=True)
subprocess.run(['git','-C',str(app),'commit','-qm','Fixture for rules and adjacent helpers'],check=True)
config='[project]\nroot="app"\nrun_root="runs"\nworktree_root="trees"\nmax_context_chars=1000\n'
for i in range(1,5): config+=f'\n[[project.migration_rules]]\npath="rules/R{i}.md"\n'
config+='\n[build]\nproject="Case.csproj"\n[checker]\ncommand='+json.dumps([sys.executable,'check.py'])+'\nmax_repair_attempts=2\n[scripts]\nafter_migration=['+json.dumps([sys.executable,'break.py'])+']\n'
config+='\n[[units]]\nid="common-rules-context"\nsource="legacy/order"\nsource_encoding="utf-8"\ntarget="Pages/Orders/OrderPage.cs"\n'
(root/'harness.toml').write_text(config)
print(root)
