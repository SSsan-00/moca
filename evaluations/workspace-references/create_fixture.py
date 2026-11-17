"""Create an isolated app that needs adjacent examples and shared function definitions."""

from pathlib import Path
import json
import subprocess
import sys
import tempfile


root = Path(tempfile.mkdtemp(prefix="harness-workspace-references-")).resolve()
app = root / "app"


def write(name: str, content: str) -> None:
    destination = app / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


write(".gitignore", "bin/\nobj/\n")
write("Case.csproj", """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net9.0</TargetFramework>
    <ImplicitUsings>enable</ImplicitUsings><Nullable>enable</Nullable>
  </PropertyGroup>
</Project>
""")
write("Pages/Invoices/InvoicePage.cs", """namespace Fixture.Invoices;
public static class InvoicePage
{
    public static InvoiceRow[] Build() => [];
}
""")
write("Pages/Invoices/InvoiceRow.cs", """namespace Fixture.Invoices;
public record InvoiceRow(string Id, string Amount, string State);
""")
# This sibling shows the pipeline and namespaces, but none of the new invoice data.
write("Pages/Invoices/HistoryPage.cs", """using Fixture.Shared;
namespace Fixture.Invoices;
public static class HistoryPage
{
    public static InvoiceRow[] Build() => [Convert("history", 1903, 325, "paid")];
    private static InvoiceRow Convert(string id, long minorUnits, int discountBps, string status)
    {
        var gross = InvoicePricing.FromMinorUnits(minorUnits);
        var net = InvoicePricing.Net(gross, discountBps);
        return new InvoiceRow(id, InvoiceFormat.Money(net), InvoiceFormat.State(status));
    }
}
""")
write("Pages/inc/InvoicePricing.cs", """namespace Fixture.Shared;
public static class InvoicePricing
{
    public static int AdditionalDiscountBps { get; set; }
    public static decimal FromMinorUnits(long minorUnits) => minorUnits / 100m;
    // Basis points, not percent. Round only after applying the complete discount.
    public static decimal Net(decimal gross, int discountBps) => decimal.Round(
        gross * (10000 - discountBps - AdditionalDiscountBps) / 10000m,
        2, MidpointRounding.AwayFromZero);
    // Preview screens deliberately show gross amounts; finalized invoices must use Net.
    public static decimal PreviewNet(decimal gross, int discountBps) => gross;
}
""")
write("Pages/inc/Formatting/InvoiceFormat.cs", """using System.Globalization;
namespace Fixture.Shared;
public static class InvoiceFormat
{
    public static string Language { get; set; } = "ja";
    public static string Money(decimal amount) => amount.ToString("0.00", CultureInfo.InvariantCulture);
    public static string State(string status) => (Language, status) switch
    {
        ("ja", "pending") => "未確定",
        ("ja", "paid") => "支払済",
        ("ja", "reversed") => "取消",
        (_, "pending") => "Pending",
        (_, "paid") => "Paid",
        (_, "reversed") => "Reversed",
        _ => throw new ArgumentOutOfRangeException(nameof(status))
    };
}
""")
rows = [
    ("INV<&01", 1005, 500, "pending"),  # Halfway rounding, positive and negative.
    ("返金02", -1005, 500, "reversed"),
    ("INV03", 99999, 123, "paid"),
    ("INV04", 1, 5000, "pending"),
    ("INV05", -1, 5000, "reversed"),
    ("INV06", 0, 0, "paid"),
    ("INV07", 1234567, 999, "paid"),
    ("INV08", 875, 333, "pending"),
    ("INV08", 875, 333, "pending"),  # Preserve duplicates and order.
    ("INV10", 3210, 2500, "paid"),
    ("INV11", 1585, 200, "pending"),
    ("INV12", -2111, 1725, "reversed"),
]
write("legacy/invoice", "<?php\nrequire_once '../inc/invoice.php';\n$rows = [\n" +
      "".join("    " + json.dumps(row, ensure_ascii=False) + ",\n" for row in rows) +
      "];\n$result = [];\nforeach ($rows as [$id, $minor, $discount, $status]) {\n"
      "    $result[] = ['Id' => $id, 'Amount' => money(net(from_minor($minor), $discount)), "
      "'State' => state_label($status)];\n}\necho json_encode($result);\n")
# Optional executable PHP baseline: run `php invoice` with legacy/ as the current directory.
# The agent sees the target-side C# equivalents; this file is outside their reference folders.
write("inc/invoice.php", """<?php
function from_minor(int $minor): float { return $minor / 100; }
function net(float $gross, int $discount): float {
    $extra = (int) getenv('EXTRA_BPS');
    return round($gross * (10000 - $discount - $extra) / 10000, 2, PHP_ROUND_HALF_UP);
}
function money(float $amount): string {
    return number_format($amount == 0 ? 0 : $amount, 2, '.', '');
}
function state_label(string $status): string {
    $japanese = (getenv('DISPLAY_LANGUAGE') ?: 'ja') === 'ja';
    return match ($status) {
        'pending' => $japanese ? '未確定' : 'Pending',
        'paid' => $japanese ? '支払済' : 'Paid',
        'reversed' => $japanese ? '取消' : 'Reversed',
        default => throw new InvalidArgumentException($status)
    };
}
""")
write("rules/common.md", """# Invoice migration
Implement the existing InvoicePage.Build() contract. Preserve all original data, duplicates and order.
Follow the similar finalized page in the same target directory. Inspect the common function definitions
in the sibling inc directory (including subdirectories), and use the existing shared API rather than
copying its calculations or text into the target. Preserve the configurable discount and display language.
These are finalized invoices, not preview screens. Edit only the configured target.
""")
write("Program.cs", """using Fixture.Invoices;
using Fixture.Shared;
var results = new List<object>();
foreach (var extra in new[] { 0, 125 })
    foreach (var language in new[] { "ja", "en" })
    {
        InvoicePricing.AdditionalDiscountBps = extra;
        InvoiceFormat.Language = language;
        results.Add(new { Extra = extra, Language = language, Rows = InvoicePage.Build() });
    }
Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(results));
""")
# The independent Python oracle is outside the material directories and unavailable to Junie.
write("rows.json", json.dumps(rows, ensure_ascii=False))
write("check.py", """import json, pathlib, re, subprocess, sys
from decimal import Decimal, ROUND_HALF_UP
rows = json.loads(pathlib.Path('rows.json').read_text(encoding='utf-8'))
actual = json.loads(subprocess.check_output(['dotnet', 'run', '--no-build'], text=True, encoding='utf-8'))
expected = []
for extra in (0, 125):
    for language in ('ja', 'en'):
        labels = dict(zip(('pending', 'paid', 'reversed'),
                         ('未確定', '支払済', '取消') if language == 'ja' else ('Pending', 'Paid', 'Reversed')))
        result = []
        for ident, minor, discount, status in rows:
            amount = (Decimal(minor) * Decimal(10000 - discount - extra) / 1000000).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP)
            if not amount:
                amount = abs(amount)
            result.append({'Id': ident, 'Amount': format(amount, '.2f'), 'State': labels[status]})
        expected.append({'Extra': extra, 'Language': language, 'Rows': result})
errors = []
if actual != expected:
    errors.append('DATA Pages/Invoices/InvoicePage.cs:1 warning: finalized invoice output differs from the original contract; inspect the sibling example and shared definitions in inc, including rounding, policy and language behavior')
source = pathlib.Path('Pages/Invoices/InvoicePage.cs').read_text(encoding='utf-8')
if re.search(r'\\bPreviewNet\\s*\\(', source):
    errors.append('POLICY Pages/Invoices/InvoicePage.cs:1 warning: finalized invoices are using preview behavior')
if errors:
    print('\\n'.join(errors))
sys.exit(bool(errors))
""")
# Force a compiling semantic error so the real checker, not a manual prompt, starts repair.
write("inject_preview.py", """from pathlib import Path
import re
p = Path('Pages/Invoices/InvoicePage.cs')
source = p.read_text(encoding='utf-8')
changed, count = re.subn(r'\\bNet\\s*\\(', 'PreviewNet(', source)
if not count:
    raise RuntimeError('Fixture expected use of the existing Net shared function before fault injection')
p.write_text(changed, encoding='utf-8')
print('Injected preview calculation into the finalized invoice target')
""")
subprocess.run(["git", "init", "-q", str(app)], check=True)
for key, value in (("user.name", "Harness validation"), ("user.email", "harness@example.invalid"), ("commit.gpgsign", "false")):
    subprocess.run(["git", "-C", str(app), "config", key, value], check=True)
subprocess.run(["git", "-C", str(app), "add", "."], check=True)
subprocess.run(["git", "-C", str(app), "commit", "-qm", "Fixture for sibling page and shared functions"], check=True)
config = '''[project]
root = "app"
run_root = "runs"
worktree_root = "trees"
[[project.migration_rules]]
path = "rules/common.md"
[build]
project = "Case.csproj"
[checker]
command = ''' + json.dumps([sys.executable, "check.py"]) + '''
max_repair_attempts = 2
[scripts]
after_migration = [''' + json.dumps([sys.executable, "inject_preview.py"]) + ''']
[[units]]
id = "workspace-references"
source = "legacy/invoice"
source_encoding = "utf-8"
target = "Pages/Invoices/InvoicePage.cs"
'''
(root / "harness.toml").write_text(config, encoding="utf-8")
print(root)
