"""Create a reproducible 20,000-line legacy orders fixture for a real Run."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
BUSINESS_SPEC = importlib.util.spec_from_file_location(
    "business_fixture", HERE.parent / "business-php" / "create_fixture.py"
)
business = importlib.util.module_from_spec(BUSINESS_SPEC)
BUSINESS_SPEC.loader.exec_module(business)


def request(**updates):
    return dict(tenant="east", customer="gold", sku="food", order_id="order-1",
                operation="quote", policy=0, quantity=2, channel="retail", coupon=None,
                delivery="standard", payment="card", today="2026-11-20", note=None,
                fail_at="", amount=-1) | updates


def scenarios(policies):
    result = {}
    for p in policies:
        n = p["NUMBER"]
        result[f"contract-{n:03d}"] = [
            request(policy=n, quantity=p["BULK_MIN"], coupon=p["COUPON"]),
            request(policy=n, customer="silver", quantity=8, sku="regular"),
            request(policy=n, customer="gold", sku="regular", quantity=p["MAX_QTY"], delivery="express"),
            request(policy=n, customer="silver", delivery="pickup", payment="cod"),
            request(policy=n, channel="wholesale", payment="credit"),
        ]
    result.update({
        "purchase-refund": [
            request(operation="place", note="返品 ' ﷺ \\ ✨"),
            request(operation="place", note="ignored on retry"),
            request(operation="place", quantity=3),
            request(operation="pay", amount=1),
            request(operation="cancel"),
            request(operation="pay"), request(operation="pay"),
            request(operation="summary"),
            request(operation="refund", fail_at="after_ledger"),
            request(operation="summary"),
            request(operation="refund"), request(operation="refund"),
            request(operation="summary"),
        ],
        "rollback-cancel": [
            request(operation="place", fail_at="after_stock", note="rollback"),
            request(operation="place", note=""),
            request(operation="pay", fail_at="after_ledger"),
            request(operation="pay", amount=9_000_000_000),
            request(operation="cancel"), request(operation="cancel"), request(operation="summary"),
        ],
        "tenants-stock-64-bit": [
            request(operation="place", sku="expensive", quantity=1),
            request(tenant="west", operation="place", sku="expensive", quantity=1),
            request(operation="pay"), request(tenant="west", operation="summary"),
            request(tenant="west", operation="place", order_id="order-2"),
            request(operation="summary"),
        ],
        "invalid-boundaries": [
            request(customer="missing"), request(customer="blocked"), request(sku="inactive"),
            request(policy=-1), request(quantity=0), request(sku="restricted", customer="silver"),
            request(coupon="期限外", today="2026-11-21"),
            request(channel="wholesale", today="2099-01-01"), request(delivery="unknown"),
            request(payment="unknown"), request(sku="expensive", payment="cod"),
            request(customer="silver", payment="credit"), request(operation="pay"),
            request(operation="other"),
        ],
    })
    return [{"id": label, "requests": steps} for label, steps in result.items()]


def create(root: Path, php: Path):
    app = root / "app"
    app.mkdir()
    for folder in ("legacy", "Pages/Business", "Pages/inc", "verification", "rules"):
        (app / folder).mkdir(parents=True)
    validation = root / "validation"
    validation.mkdir()

    def write(name, text):
        (app / name).write_text(text, encoding="utf-8", newline="\n")

    php_source, policies = business.generate_php(20_000)
    source_data = php_source.encode("utf-8")
    (app / "legacy/business.php").write_bytes(source_data)
    write(".gitignore", "bin/\nobj/\n")
    for name, destination in (
        ("Program.cs", "verification/Program.cs"),
        ("Case.csproj", "Case.csproj"),
        ("schema.sql", "verification/schema.sql"),
    ):
        shutil.copyfile(HERE / name, app / destination)
    write("Pages/inc/Contracts.cs",
          "namespace Migration;\n\npublic sealed record Request(\n"
          "    string Tenant, string Customer, string Sku, string OrderId,\n"
          "    string Operation, int Policy, int Quantity, string Channel,\n"
          "    string? Coupon, string Delivery, string Payment, string Today,\n"
          "    string? Note, string FailAt, long Amount);\n\n"
          "public sealed record PolicySpec(\n    " + ",\n    ".join(
              f"{kind} {name}" for name, _, kind in business.POLICY_FIELDS) + ");\n")
    write("Pages/Business/BusinessService.cs",
          "using Microsoft.Data.Sqlite;\nnamespace Migration;\n"
          'public static class BusinessService { public static object Handle(SqliteConnection db, Request r) => new { ok = false, code = "unimplemented" }; }\n')
    write("Pages/Business/PolicyCatalog.cs", "namespace Migration;\npublic static class PolicyCatalog { }\n")
    shutil.copyfile(HERE / "source_runner.php", validation / "source_runner.php")
    request_cases = scenarios(policies)
    cases = validation / "requests.json"
    cases.write_text(json.dumps(request_cases, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    command = [str(php), "-n", "-d", "display_errors=stderr", "-d", f"extension_dir={php.parent / 'ext'}",
               "-d", "extension=pdo_sqlite"]
    subprocess.run([*command, "-l", str(app / "legacy/business.php")], check=True)
    run = subprocess.run([*command, str(validation / "source_runner.php"),
                          str(app / "legacy/business.php"), str(app / "verification/schema.sql"),
                          str(cases)], check=True, capture_output=True)
    if run.stderr:
        raise RuntimeError(run.stderr.decode("utf-8", "replace"))
    responses = json.loads(run.stdout)
    count = sum(len(case["requests"]) for case in request_cases)
    assert sum(len(case["steps"]) for case in responses) == count
    assert len(responses) == len(request_cases)
    assert responses[0]["steps"][0]["ok"] is True
    examples = {case["id"]: case for case in responses}
    refund = examples["purchase-refund"]
    assert [step["code"] for step in refund["steps"]] == [
        "open", "open", "idempotency_conflict", "partial", "not_cancellable", "paid",
        "not_payable", "summary", "injected_failure", "summary", "refunded", "refunded", "summary",
    ]
    assert [entry["kind"] for entry in refund["state"]["ledger"]] == ["payment", "payment", "refund"]
    assert sum(entry["amount"] for entry in refund["state"]["ledger"]) == 0
    assert examples["rollback-cancel"]["state"]["ledger"] == []
    assert len(examples["rollback-cancel"]["state"]["events"]) == 2
    tenant = examples["tenants-stock-64-bit"]
    assert tenant["steps"][0]["amount"] > 2**31
    assert tenant["steps"][4]["code"] == "stock_shortage"
    expected = validation / "expected.json"
    expected.write_bytes(run.stdout)

    docs = [
        ("required-api.md", "required", "", """# API and source fidelity
Implement Migration.BusinessService.Handle(SqliteConnection db, Request r) in BusinessService.cs. Another target, PolicyCatalog.cs, may use the provided PolicySpec record for a compile-time catalog covering every PHP policy. Return JSON identical in keys, value types, nulls and values. Use integer money with C# long for monetary quantities and products. Preserve PHP operation order and exact strings. The provided .NET 9 fixture has a read-write in-memory SQLite database. Do not execute project or migration code yourself; the separate configured checker does that. Never read the original PHP from target code, hard-code outputs, modify the verifier or access its oracle, or write outside declared targets and scratch. Source is 20,000 lines: multiple capsules are consecutive line ranges of a single file, not independent programs. Inspect all of them and carry forward the existing target code. Where declarations are split, use the preserved working target, capsule boundaries and staged parser as appropriate; never guess coefficients. Shared code and programmatically extracted literal parameters are encouraged when their equivalence is checked. Do not replicate hundreds of PHP functions verbatim if a shared calculation and explicit typed constants preserve behavior.
"""),
        ("required-db.md", "required", "", """# SQLite and business integrity
Parameterize SQL, preserve repeated named parameters, pass each transaction to its commands, and bind null as DBNull.Value. Preserve tenant/store isolation, same-order retries, transaction rollback on injected failures and signed event/ledger amounts. The checker compares the actual return from every operation and the final five mutable tables plus memberships, including ordering, Unicode and null. Preserve the public request and method signatures.
"""),
        ("detail-money.md", "reference", "Relevant to money_ratio, discounts, coupons, taxes or shipping.",
         "# Monetary rules\nKeep floor/ceil/half rounding and reason suffix ordering. Preserve policy exceptions and thresholds from every quote function, including late-file contracts.\n"),
        ("detail-ledger.md", "reference", "Relevant to pay, cancel, refund, audit, ledger, or inventory updates.",
         "# Ledger and cancellation\nPreserve state transitions, signed ledger/audit writes, retries without double updates, and rollback when a simulated write fails.\n"),
        ("detail-mail.md", "reference", "Only applicable to SMTP delivery, not order lifecycle or database changes.",
         "# SMTP delivery\nRetain existing SMTP failure retry behavior only when applicable. Do not add mail delivery to order or price logic.\n"),
    ]
    config = '[project]\nroot="app"\nrun_root="runs"\nworktree_root="trees"\n'
    for name, usage, description, contents in docs:
        write("rules/" + name, contents)
        config += f'\n[[project.migration_rules]]\npath="rules/{name}"\nusage="{usage}"\n'
        if description:
            config += "description=" + json.dumps(description) + "\n"
    config += '\n[junie]\nmodel="gpt-6-luna"\neffort="medium"\n'
    config += '\n[build]\nproject="Case.csproj"\n[checker]\ncommand=' + json.dumps(
        [sys.executable, str(HERE / "check.py"), str(cases), str(expected)]) + '\nmax_repair_attempts=2\n'
    config += ('\n[[units]]\nid="business-scale"\nsource="legacy/business.php"\nsource_encoding="utf-8"\n'
               'targets=["Pages/Business/BusinessService.cs","Pages/Business/PolicyCatalog.cs"]\n')
    (root / "harness.toml").write_text(config, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(app)], check=True)
    subprocess.run(["git", "-C", str(app), "add", "."], check=True)
    subprocess.run(["git", "-C", str(app), "-c", "user.name=Harness Evaluation", "-c",
                    "user.email=harness@example.invalid", "-c", "commit.gpgsign=false", "commit",
                    "-qm", "Seed synthetic order fixture"], check=True)
    manifest = dict(php_lines=len(php_source.splitlines()), php_chars=len(php_source),
                    php_sha256=hashlib.sha256(source_data).hexdigest(),
                    scenario_sha256=hashlib.sha256(cases.read_bytes()).hexdigest(),
                    oracle_sha256=hashlib.sha256(expected.read_bytes()).hexdigest(), policies=len(policies),
                    scenarios=len(request_cases), operations=count,
                    expected_selected_details=["rule-0003.txt", "rule-0004.txt"])
    (root / "fixture.json").write_text(json.dumps(manifest, indent=2)+"\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--php", default="php", help="PHP executable with PDO SQLite")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    executable = shutil.which(args.php)
    if not executable:
        parser.error("PHP was not found")
    root = args.output or Path(tempfile.mkdtemp(prefix="harness-business-20k-"))
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = create(root, Path(executable).resolve())
    print(root)
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
