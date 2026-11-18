"""Create business PHP and documents for static-only migration validation.

Generated policy functions model copied per-contract legacy implementations.
They are deliberately repetitive; this is not a real production repository.
"""
import hashlib
import json
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent


def parameters(number):
    """評価ケースの番号から繰り返し可能な入力値を作る。"""
    rng = random.Random(f"business-php-v1-{number}")
    return dict(
        ID=f"{number:03d}", NUMBER=number, MAX_QTY=rng.randrange(25, 61), PROGRAM=f"club-{number:03d}",
        TAG=hashlib.sha256(f"contract-{number}".encode()).hexdigest()[:8],
        GOLD=rng.randrange(401, 1700), SILVER=rng.randrange(100, 400), SILVER_MIN=rng.randrange(1000, 5000),
        BULK_MIN=rng.randrange(4, 14), BULK_RATE=rng.randrange(500, 1900), ROUND=rng.choice(["floor", "ceil", "half"]),
        COUPON=f"会員'{number:03d}\\特典", EXPIRY="2026-11-20", COUPON_MIN=rng.randrange(2500, 4000),
        COUPON_AMOUNT=rng.randrange(400, 1400), POINTS_MIN=rng.randrange(50, 500), POINTS_AMOUNT=rng.randrange(10, 900),
        DISCOUNT_CAP=rng.randrange(2000, 4000), SHIPPING=rng.randrange(400, 700), EXPRESS=rng.randrange(500, 900),
        ISLAND_BLOCK=rng.choice(["true", "false"]), ISLAND=rng.randrange(900, 1600), FREE_SHIP=rng.randrange(8000, 12000),
        FOOD_TAX=800, TAX=1000, COD_LIMIT=rng.randrange(15000, 20000), COD_FEE=rng.randrange(200, 500),
    )


def generate_php():
    """固定ケースを展開し、評価用PHPと期待データを生成する。"""
    core = (HERE / "core.php").read_text(encoding="utf-8")
    template = (HERE / "policy.php.tpl").read_text(encoding="utf-8")
    policies = []
    blocks = []
    # Each contract adds a real function and a dispatch entry, not dummy rows.
    count = (2_000 - len(core.splitlines()) - 4) // (len(template.splitlines()) + 1)
    for number in range(count):
        p = parameters(number)
        policies.append(p)
        def substitute(match):
            """参照用のサンプルと資料にケース固有の値を埋め込む。"""
            return str(p[match[1]]).replace("\\", "\\\\").replace("'", "\\'")
        blocks.append(re.sub(r"@([A-Z_]+)@", substitute, template))
    dispatch = "function policy_functions(): array {\n    return [\n" + "".join(
        f"        {number} => 'quote_{number:03d}',\n" for number in range(count)
    ) + "    ];\n}\n"
    source = core + "".join(blocks) + dispatch
    # Normal multiline SQL parameter formatting makes the physical size exactly 2k.
    remainder = 2_000 - len(source.splitlines())
    assert 0 <= remainder < len(template.splitlines()) + 1
    source = re.sub(r", (?=':[a-z]+'\s*=>)", ",\n        ", source, count=remainder)
    assert len(source.splitlines()) == 2_000
    return source, policies


POLICY_FIELDS = [
    ("Id", "NUMBER", "int"), ("MaxQty", "MAX_QTY", "int"), ("Program", "PROGRAM", "string"),
    ("Tag", "TAG", "string"), ("Gold", "GOLD", "int"), ("SilverMin", "SILVER_MIN", "long"),
    ("Silver", "SILVER", "int"), ("BulkMin", "BULK_MIN", "int"), ("BulkRate", "BULK_RATE", "int"),
    ("Rounding", "ROUND", "string"), ("Coupon", "COUPON", "string"), ("Expiry", "EXPIRY", "string"),
    ("CouponMin", "COUPON_MIN", "long"), ("CouponAmount", "COUPON_AMOUNT", "long"),
    ("PointsMin", "POINTS_MIN", "int"), ("PointsAmount", "POINTS_AMOUNT", "long"),
    ("DiscountCap", "DISCOUNT_CAP", "int"), ("Shipping", "SHIPPING", "long"),
    ("Express", "EXPRESS", "long"), ("IslandBlock", "ISLAND_BLOCK", "bool"), ("Island", "ISLAND", "long"),
    ("FreeShip", "FREE_SHIP", "long"), ("FoodTax", "FOOD_TAX", "int"), ("Tax", "TAX", "int"),
    ("CodLimit", "COD_LIMIT", "long"), ("CodFee", "COD_FEE", "long"),
]


DETAILS = {
    9: ("SQL parameters", "SQL values must use parameters, including quotes and Unicode; preserve named parameter reuse."),
    27: ("Tenant isolation", "Every customer, inventory, membership, order and ledger query is tenant-scoped. Duplicate IDs across shops must stay independent."),
    44: ("Money rounding", "Preserve integer minor units, each contract's floor/ceil/half rounding, discount cap and the exact order of discount, shipping, tax and COD fees."),
    61: ("Membership and coupons", "Preserve nullable/missing membership, date equality, exact coupon strings, points thresholds and wholesale exclusions."),
    78: ("Database transactions", "All writes in an order operation must commit together or roll back together, including inventory, payments, balance and audit rows."),
    95: ("Order lifecycle", "Preserve open/partial/paid/cancelled/refunded transitions, forbidden transitions, overpayment checks and partial payment amounts."),
    112: ("Idempotency", "Repeated place/cancel/refund requests follow the original semantics without duplicate inventory movements or audit rows. Conflicting order reuse is rejected."),
    129: ("Null and Unicode", "Preserve null versus empty notes/coupons, apostrophes, literal backslashes and emoji in returned data and stored SQL values."),
    146: ("Audit and summaries", "Preserve event order and signed ledger entries. Customer summaries exclude cancelled/refunded orders and sum outstanding amounts."),
    181: ("Contract variations", "Inspect every contract's executable branches and constants, including exceptions in later source capsules. Generalization is allowed only if all original behavior is preserved."),
}


def create():
    """合成PHP、資料、期待値を評価用アプリへ配置する。"""
    started = time.monotonic()
    root = Path(tempfile.mkdtemp(prefix="harness-business-")).resolve()
    app = root / "app"
    for directory in ("Pages/Business", "Pages/inc", "legacy", "rules", "verification"):
        (app / directory).mkdir(parents=True)
    def write(name, text):
        """評価用アプリの相対パスへUTF-8の本文を書く。"""
        (app / name).write_text(text, encoding="utf-8", newline="")
    source, policies = generate_php()
    write("legacy/business.php", source)
    write(".gitignore", "bin/\nobj/\nverification/last-check.json\n")
    write("Case.csproj", '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net10.0</TargetFramework><ImplicitUsings>enable</ImplicitUsings><Nullable>enable</Nullable></PropertyGroup><ItemGroup><Compile Remove="verification/**/*.cs" /><PackageReference Include="Microsoft.Data.Sqlite.Core" Version="10.0.13" /></ItemGroup></Project>\n')
    for filename, destination in (("Contracts.cs", "Pages/inc/Contracts.cs"), ("check.py", "verification/check.py"),
                                  ("Inspect.csproj", "verification/inspector/Inspect.csproj"),
                                  ("Inspect.cs", "verification/inspector/Program.cs")):
        (app / destination).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(HERE / filename, app / destination)
    with (app / "Pages/inc/Contracts.cs").open("a", encoding="utf-8") as stream:
        stream.write("\npublic sealed record PolicySpec(\n    " + ",\n    ".join(
            f"{kind} {name}" for name, _, kind in POLICY_FIELDS) + ");\n")
    write("Pages/Business/BusinessService.cs", 'using Microsoft.Data.Sqlite;\nnamespace Migration;\npublic static class BusinessService { public static object Handle(SqliteConnection db, Request r) => new { ok = false, code = "unimplemented" }; }\n')
    write("Pages/Business/PolicyCatalog.cs", 'namespace Migration;\npublic static class PolicyCatalog { }\n')
    rules = [("required-api.md", "required", "", "Implement Migration.BusinessService.Handle(Microsoft.Data.Sqlite.SqliteConnection db, Migration.Request r) returning an object whose JSON has the same keys/types/values as PHP business(). The connection is open. Request is provided in Pages/inc/Contracts.cs and every request field is part of the public contract. Targets are BusinessService.cs and PolicyCatalog.cs; put any additional types in these two files. Namespace Migration; .NET 10. Microsoft.Data.Sqlite 10.0.13 is available in the real build. Return dictionaries or anonymous objects with the original lowercase keys; do not change the public API. Preserve all original contract behavior, SQL side effects, errors and transaction boundaries. The provided PolicySpec record is a fixed public data contract. Declare public static readonly PolicySpec[] Policies in PolicyCatalog, with one statically inspectable new PolicySpec(...) entry for every quote_N function. Preserve every field from the PHP; no runtime decoding of opaque data blobs. Monetary intermediates should retain PHP 64-bit integer semantics. DBNull.Value is required when binding a null SQL value, and every command inside a transaction must participate in that transaction.\n"),
             ("required-integrity.md", "required", "", "Migrate all executable PHP functions and all per-contract constants. You may consolidate repeated code into data plus shared C# functions only after extracting and checking every contract. Do not call PHP, use an interpreter, read legacy files at runtime, hard-code test outputs or omit branches. Do not implement unrelated features. Validation is static only: do not execute PHP, the migrated application, or connect to any application database. The checker inspects C# syntax, policy literals and SQL structure without invoking business code. This does not establish runtime equivalence. Intermediate capsules may have incomplete dispatch; the final capsule must preserve and verify all earlier work. Temporary files belong in the scratch directory provided by the harness.\n")]
    unrelated = ["SMTP mail delivery", "PDF generation", "SFTP synchronization", "image resizing", "OAuth browser login", "WebSocket transport", "ZIP extraction", "video streaming", "DNS provisioning", "calendar reminders"]
    for number in range(1, 199):
        if number in DETAILS:
            title, body = DETAILS[number]
            description = f"Applicable when the PHP includes {title.lower()} or equivalent behavior."
            text = f"# {title}\n{body}\n"
        else:
            title = unrelated[number % len(unrelated)]
            description = f"Procedure {number:03d}, only for {title}. No requirements for local order/inventory/payment logic without that feature."
            text = f"# {title} procedure {number:03d}\n" + "".join(f"Step {i:02d}: preserve the existing {title} timeout, failure and cleanup behavior; do not add this feature to unrelated applications.\n" for i in range(1, 49))
        rules.append((f"detail-{number:03d}.md", "reference", description, text))
    for name, _, _, text in rules:
        write("rules/" + name, text)
    expected = [{name: (p[key] == "true" if kind == "bool" else p[key])
                 for name, key, kind in POLICY_FIELDS} for p in policies]
    write("verification/expected-policies.json", json.dumps(expected, ensure_ascii=False, indent=2) + "\n")
    config = '[project]\nroot="app"\nrun_root="runs"\nworktree_root="trees"\n'
    for name, usage, description, _ in rules:
        config += '\n[[project.migration_rules]]\npath=' + json.dumps("rules/" + name) + '\nusage=' + json.dumps(usage) + '\n'
        if description:
            config += 'description=' + json.dumps(description) + '\n'
    config += '\n[build]\nproject="Case.csproj"\n[checker]\ncommand=' + json.dumps([sys.executable, "verification/check.py"]) + '\nmax_repair_attempts=2\n'
    config += '\n[[units]]\nid="business-scale"\nsource="legacy/business.php"\nsource_encoding="utf-8"\ntargets=["Pages/Business/BusinessService.cs","Pages/Business/PolicyCatalog.cs"]\n'
    (root / "harness.toml").write_text(config, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(app)], check=True)
    subprocess.run(["git", "-C", str(app), "add", "."], check=True)
    subprocess.run(["git", "-C", str(app), "-c", "user.name=Harness validation", "-c", "user.email=harness@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "Business PHP and static validation"], check=True)
    fixture = dict(php_lines=len(source.splitlines()), php_bytes=len(source.encode()), policies=len(policies),
        functions=len(re.findall(r"(?m)^function ", source)), conditional_statements=len(re.findall(r"(?m)^\s*(?:if|elseif|else)\b", source)),
        validation="static_only", application_executed=False, database_accessed=False,
        documents=len(rules), document_bytes=sum(len(t.encode()) for _, _, _, t in rules), expected_selected_details=[f"rule-{i + 2:04d}.txt" for i in DETAILS],
        php_sha256=hashlib.sha256(source.encode()).hexdigest(),
        policy_contract_sha256=hashlib.sha256((app / "verification/expected-policies.json").read_bytes()).hexdigest(),
        generation_seconds=round(time.monotonic()-started, 3))
    (root / "fixture.json").write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(root)


if __name__ == "__main__":
    create()
