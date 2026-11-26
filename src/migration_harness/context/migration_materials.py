from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from . import php_literals
from .. import files as harness_files
from ..execution import check_cancelled, report_activity
from ..files import safe_target
from ..rules.rule_materials import detail_index, render_guidelines
from ..runs.worktree_targets import target_hash
from .context_evidence import boundary_hints


def stage_source_excerpts(
    capsule: Path, manifest: dict[str, Any], agent_root: Path, *, through_capsule: int | None = None,
) -> dict[Path, str]:
    """処理済みの原文を照合用に残す。将来の区間や境界資料は混ぜない。"""
    directory = safe_target(agent_root, Path(".migration-materials/source-0001.md")).parent
    directory.mkdir(exist_ok=True)
    parts = manifest["parts"][:through_capsule]
    names = {f"source-{part['index']:04d}.md" for part in parts}
    for path in directory.glob("source-*.md"):
        if path.name not in names:
            safe_target(agent_root, path.relative_to(agent_root)).unlink()
    verified = {}
    for part in parts:
        check_cancelled()
        source = Path(part["path"])
        if not source.is_absolute() or not source.is_relative_to(capsule):
            raise ValueError("saved verification source is missing or outside the capsule directory")
        source = safe_target(capsule, source.relative_to(capsule))
        if not source.is_file():
            raise ValueError("saved verification source is missing or outside the capsule directory")
        destination = safe_target(agent_root, Path(".migration-materials") / f"source-{part['index']:04d}.md")
        existing = target_hash(destination)
        if part.get("segment_sha256") and part.get("sha256") and existing == part["segment_sha256"]:
            if harness_files.sha256_file(source) != part["sha256"]:
                raise ValueError(f"capsule {part['index']}: saved verification source changed")
            verified[destination.relative_to(agent_root)] = existing
            continue
        data = source.read_bytes()
        if part.get("sha256") and hashlib.sha256(data).hexdigest() != part["sha256"]:
            raise ValueError(f"capsule {part['index']}: saved verification source changed")
        original = data.decode("utf-8")[manifest.get("segment_offset", 0):].encode("utf-8")
        digest = hashlib.sha256(original).hexdigest()
        if part.get("segment_sha256", digest) != digest:
            raise ValueError(f"capsule {part['index']}: saved verification excerpt changed")
        if existing != digest:
            destination.write_bytes(original)
        verified[destination.relative_to(agent_root)] = digest
    return verified


def stage_migration_materials(
    capsule: Path, manifest: dict[str, Any], agent_root: Path, *,
    targets: tuple[Path, ...] = (), selected: frozenset[str] | None = None, repair: bool = False,
    supplied_rules: list[dict] | None = None, excluded: frozenset[str] = frozenset(),
) -> str:
    """準備時の資料を照合して、今回のJunieに読む順番を伝える。"""
    materials = manifest.get("materials", {})
    omitted = set(excluded)
    # Noneは選別なし、空集合は「該当する詳細なし」。後者では前回置いた詳細も取り除く。
    if selected is not None:
        omitted.update({item["file"] for item in detail_index(manifest)} - selected)
        omitted.add("rules.json")
    for name, digest in materials.items():
        if not re.fullmatch(r"shared\.md|references\.json|rules\.json|(?:reference|rule)-\d+\.txt", name):
            raise ValueError("invalid migration material filename")
        source = safe_target(capsule, Path("materials") / name)
        destination = safe_target(agent_root, Path(".migration-materials") / name)
        if target_hash(source) != digest or not source.is_file():
            raise ValueError(f"saved migration material changed or is missing: {name}")
        if name in omitted:
            destination.unlink(missing_ok=True)
            continue
        if target_hash(destination) != digest:
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, destination)
    instructions = ""
    guidelines = safe_target(agent_root, Path(".migration-materials/guidelines.md"))
    text = render_guidelines(capsule / "materials", manifest, targets=targets, selected=selected,
                             supplied_rules=supplied_rules, excluded=excluded)
    if text:
        guidelines.parent.mkdir(exist_ok=True)
        guidelines.write_text(text, encoding="utf-8", newline="")
        instructions += (
            'Before making any edit, review the Junie guidelines for mandatory rules and any supplied target '
            'details. '
        )
        if selected is None:
            instructions += 'If there is a detail index, read applicable saved files before making edits. '
        report_activity(f"必須事項・該当する詳細・資料の索引を起動時の指示に設定しました（{len(text):,}文字）。")
    else:
        guidelines.unlink(missing_ok=True)
    helper = safe_target(agent_root, Path(".migration-materials/php_literals.py"))
    helper.parent.mkdir(exist_ok=True)
    shutil.copyfile(Path(php_literals.__file__), helper)
    scratch = agent_root / ".migration-scratch"
    if scratch.is_symlink():
        raise ValueError("scratch directory must not be a symbolic link")
    scratch.mkdir(exist_ok=True)
    if "shared.md" in materials and ("rule_files" in manifest or "rule_chunks" in manifest):
        instructions += 'Before editing, read `.migration-materials/shared.md` and apply its constraints. '
    if "references.json" in materials:
        instructions += (
            'Before editing, read `.migration-materials/references.json`. Each PHP example and its following '
            'C#/Razor files form a pair through the next PHP entry. Identify the applicable pair and read the '
            'relevant lines on both sides in small ranges. Follow their implementation patterns. '
            'Keep the migrated literals and behavior from the actual PHP, including when examples use different data. '
        )
    if repair and "materials" not in manifest and manifest.get("shared_sources"):
        source = Path(manifest["parts"][0]["path"]).resolve()
        if not source.is_relative_to(capsule) or not source.is_file():
            raise ValueError("saved shared context is missing or outside the capsule directory")
        destination = safe_target(agent_root, Path(".migration-materials/legacy.md"))
        destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, destination)
        instructions += 'Before editing, read the Shared context section in `.migration-materials/legacy.md` and apply its rules and examples. '
    if repair:
        stage_source_excerpts(capsule, manifest, agent_root)
        instructions += (
            'Use the numbered original PHP excerpts in `.migration-materials/source-*.md` to verify the '
            'behavior around the diagnostic. Search and read relevant lines in small ranges. Preserve existing '
            'values and output unless the actual PHP requires a change. '
        )
    return instructions


CSHARP_IDENTIFIERS = (
    "Rename only a declaration from PHP or draft C# whose identifier would be an illegal C# keyword in its "
    "actual usage. Choose a meaningful non-keyword name unused in its scope. Do not escape the keyword with @ "
    "or Unicode to solve the conflict. Change only references bound to that declaration, including later "
    "capsules and the paired Razor file. Keep ordinary names, independently declared same-spelled symbols, "
    "existing valid escapes and legal contextual names exactly as they are; do not rename for style or to "
    "free a preferred name. For example, if eventName is occupied, keep it and pick a different name for event. "
    "Preserve external names at their existing boundary, e.g. [JsonPropertyName(\"event\")] on EventName "
    "for System.Text.Json, or the original form/query key when binding input. Keep route, SQL, HTML and "
    "JavaScript names and literals unchanged. Normal Razor transitions such as @Model.EventName remain valid. "
    "Do not globally replace text, change an external C# API, or silently break uneditable callers; ask for "
    "review if the contract cannot be preserved within the allowed targets. "
)


TARGET_ENCODING = (
    "Save edited targets as UTF-8 text. The harness validates UTF-8 and adds the BOM before applying "
    "the targets, so do not run encoding or BOM checks or create a hex dump. "
)


STATIC_SOURCE_REVIEW = (
    "Verify source and target statically. Do not execute the original PHP or migrated application, "
    "or connect to an application database. Source extraction and syntax/data inspection scripts are allowed. "
    "The harness runs the configured build and checker separately. "
)


EXACT_SOURCE_DATA = (
    "For repeated statements with data, literal tables, or other long sequences, use the workspace shell to "
    "extract the exact ordered data mechanically from the assigned PHP lines. A short Python command is fine. "
    "Use the supplied helper or a read-only one-liner instead of writing extraction scripts. "
    "If temporary data is needed, put it only in .migration-scratch/ (no symlinks); it is not copied "
    "to the application. Do not leave temporary files elsewhere. "
    "Read the file on disk, select the original numbered source rows in scope, and match the complete statements "
    "or use a tokenizer as needed. Exclude examples, shared rules, and read-only boundary rows from the data. "
    "For plain quoted PHP strings use the supplied .migration-materials/php_literals.py: "
    "in Python, import runpy; helpers = runpy.run_path('.migration-materials/php_literals.py') "
    "(no import cache files). For a new ASCII identifier, helpers['csharp_identifier'](candidate, "
    "occupied=other_bound_names) checks a proposed non-keyword name and avoids collisions. "
    "For literal array appends call helpers['capsule_appends'](path, variable) "
    "with the actual source variable name. Use each saved .migration-materials/source-*.md in filename order "
    "when available, otherwise .migration-context.md; this reconstructs the assigned sequence through this segment. "
    "Never merge boundary.md into extracted data: it contains reference-only rows from adjacent capsules. "
    "The helper checks the complete assigned line range, preserves escaped "
    "quotes and rejects incomplete assignments. For other statement forms use helpers['STRING_LITERAL'] with fullmatch "
    "on the whole statement and helpers['decode_literal'] on the complete quoted token. Never use a lazy quote regex "
    "or unicode_escape. Unsupported expressions must be handled from the original code, never skipped. "
    "For generated C# primitive literals use helpers['csharp_literal'] on decoded strings, booleans, nulls and "
    "signed 64-bit integers. Do not use Python repr or raw PHP tokens: True/False and single-quoted strings "
    "are not valid C# equivalents. Handle other types explicitly from their source semantics. "
    "Keep the same values, order, duplicates, and exceptions. Never derive the sequence or its length from "
    "sampled lines or the current C# output. Prefer a compact lossless representation generated by the extraction "
    "command. For PHP single-quoted literals, unescape only a doubled backslash and a backslash-escaped "
    "apostrophe; preserve other backslashes and Unicode verbatim. Do not use a generic Unicode-escape decoder. "
    "Do not print or retype thousands of values. Before submitting, compare the decoded "
    "data or expanded sequence with the extracted source sequence, including both values and order. A matching "
    "count alone is insufficient. Decode PHP string literals according to PHP escaping and interpolation "
    "semantics, then encode their values once for the target language. Do not JSON-escape already escaped "
    "PHP spelling or compare raw literal spelling as if it were a decoded value. Verify quotes, backslashes, "
    "non-ASCII characters and duplicates after decoding. Use a standard serializer to generate complete "
    "arrays or tables, including separators, instead of assembling escaping or delimiters by hand. "
    "Replace only the data block, preserving the original prefix and suffix, including trailing methods and "
    "class/namespace closing braces. Inspect the end of the complete target after generating the block. "
    "Serialize source data only: preserve the implementation requirements in the applicable rules. "
    "If a rule requires a helper, shared constant, or API call, keep that reference in the generated code; "
    "do not precompute its current result into literals even when the current output is identical. "
    "When appending to earlier capsules, validate the entire combined expression and its boundary separator, "
    "not only the values in the new rows. Preserve earlier rule-compliant calls and constants while extending "
    "the data. Before completion, check the combined target against both mandatory and selected detail rules, "
    "including required implementation choices as well as runtime values. "
)


def razor_instructions(targets: tuple[Path, ...], *, repair: bool = False) -> str:
    if not any(path.suffix.lower() == ".cshtml" for path in targets):
        return ""
    route = (
        "Preserve the @page directive unless the diagnostic is about routing. "
        if repair else
        "Copy every existing @page line verbatim. If it is just @page, leave it as just @page: "
        "it already routes by file path. Never add an explicit route to a bare @page. "
    )
    return (
        "Check the .cshtml and .cshtml.cs together. "
        f"{route}"
        "Preserve the existing PageModel namespace. "
        "If the PageModel is in the global namespace, keep the class global and use its class name in @model. "
        "For a namespaced PageModel, use its fully qualified type in @model and verify the class and namespace match "
        "the code-behind before writing the completion record. Do not rely on unstaged Razor imports. "
        "For PHP htmlspecialchars or htmlentities used to output plain text, keep the original unencoded value in C# "
        "and output it with an ordinary Razor @ expression. Razor encodes that expression once. "
        "Do not call Html.Encode, HtmlEncoder.Encode or WebUtility.HtmlEncode before that expression, "
        "and do not use Html.Raw to bypass Razor encoding for this text. "
    )


def stage_migration_capsule(unit, capsule, manifest, agent_root, relative_targets, index,
                             context_path, capsule_count, material_instructions, *,
                             resume_first_capsule, expected_capsule_hashes):
    """保存カプセルを照合し、対象範囲と完了記録の指示を準備する。"""
    agent_context = agent_root / ".migration-context.md"
    part = manifest["parts"][index - 1]
    expected = expected_capsule_hashes[index - 1] if expected_capsule_hashes is not None else part.get("sha256")
    if expected and target_hash(Path(context_path)) != expected:
        raise ValueError(f"capsule {index}: saved context changed or is missing")
    shutil.copyfile(context_path, safe_target(agent_root, Path(agent_context.name)))
    source_instruction = ""
    verified_sources = {}
    if len(manifest["parts"]) > 1:
        verified_sources = stage_source_excerpts(capsule, manifest, agent_root, through_capsule=index)
        source_instruction = (
            "For checking accumulated data, `.migration-materials/source-*.md` contains the exact assigned "
            "excerpts from segment 1 through this segment, in filename order. Use only the assigned source "
            "headings; exclude any shared examples in legacy capsules. Verify the full accumulated sequence "
            "against these originals before completing this segment, preserving values, order, duplicates and "
            "PHP literal decoding. Use direct source and target reads for short, straightforward sequences. "
            "For long or repetitive data, use mechanical extraction and comparison as described below. "
            "Do not rely on the previous target's count or contents as the source of truth. "
            "Never append data from `.migration-boundary.md`; those rows belong to another segment. "
            "A function may span several source excerpts. Reassemble its numbered rows by original file and "
            "line across all available segments before parsing the function; do not parse each segment as "
            "a separate complete function or invent defaults for fields beyond its boundary. "
        )
    boundary_instruction = ""
    boundary = safe_target(agent_root, Path(".migration-boundary.md"))
    boundary_digest = part.get("boundary_sha256")
    if boundary_digest:
        boundary_source = Path(context_path).with_name("boundary.md")
        if not boundary_source.resolve().is_relative_to(capsule) or boundary_source.is_symlink() or target_hash(boundary_source) != boundary_digest:
            raise ValueError(f"capsule {index}: boundary context changed or is missing")
        shutil.copyfile(boundary_source, boundary)
        boundary_instruction = (
            'Read `.migration-boundary.md` before editing. Use its neighboring lines and the changes '
            'already in the targets to carry declarations, values, and control flow across this boundary. '
            'Migrate only the range assigned in the current capsule. '
        )
    else:
        boundary.unlink(missing_ok=True)
    completion_token = uuid.uuid4().hex
    # 隠しファイルはJunieが書き込み前に承認を求めるため、通常の名前を使う。
    completion_path = agent_root / f"migration-complete-{completion_token}.txt"
    resume_instruction = ""
    if resume_first_capsule:
        resume_instruction = (
            "This capsule was interrupted. Compare its source ranges with the saved target, "
            "keep completed edits, and migrate only what is still missing. "
        )
    final_review = (
        "This is the final segment. Reconcile every now-complete function against the original PHP across "
        "all saved source excerpts. Check exact branch conditions and selector strings, comparison operators, "
        "operation order and return behavior, not only the data sequence. Correct provisional logic from "
        "earlier segments, including every constant in functions split across segments, against their now-complete "
        "original bodies. Rule titles, filenames and descriptions "
        "select guidance; they are not the source of executable selector names or conditions. "
    ) if index == capsule_count and index > 1 else ""
    prompt = (
        f"Start with the headings and shared rules in `{agent_context}`; inspect source in bounded ranges. "
        f"This is context segment {index} of {capsule_count} "
        f"for migration unit `{unit['id']}`. The configured target files are "
        + ", ".join(f"`{path.as_posix()}`" for path in relative_targets) + ". "
        "Edit the current target copies directly. Do not create helper scripts to generate C#, extract source, "
        "or compare outputs. Review relevant excerpts and edits directly; the harness runs configured "
        "builds and checkers after migration. "
        "An imported draft may intentionally fail to compile. Resolve provisional types such as __UNKNOWN__, "
        "including parameter and return types, from the PHP, existing callers, and approved rules. Do not substitute "
        "a guessed type just to compile. Preserve established contracts and operator edits unless the migration or "
        "an explicit operator instruction requires a change. Leave placeholders outside this segment for later, "
        "and resolve all in-scope placeholders by the final segment. "
        "Preserve work from earlier segments and continue only with this segment. "
        "Read the current target around each edit. If a patch does not match, reread the target and make a fresh, smaller patch. "
        "Do not retry a denied action through another tool; report the blocking step if you cannot continue. "
        "Edit only the configured targets, the optional handoff file, the completion record, and temporary files in .migration-scratch/. "
        "Ensure every configured target exists before writing a completion record. "
        f"{TARGET_ENCODING}{CSHARP_IDENTIFIERS}"
        "Apply all mandatory rules in the Shared context section if present. "
        f"{material_instructions}{boundary_instruction}{source_instruction}"
        f"{boundary_hints(manifest, index)}"
        f"{resume_instruction}"
        "The source excerpt may begin or end inside a function or control structure. Use its original line numbers; "
        "do not treat the excerpt boundary as a PHP statement boundary or add closing syntax just for the excerpt end. "
        "For `focus chars START-END context chars CONTEXT_START-CONTEXT_END` records, migrate only the focus range. "
        "The surrounding context range overlaps adjacent segments and is reference-only; do not migrate it twice. "
        "These records are slices of one physical source line, and displayed line breaks are not source newlines. "
        "Do not infer text outside the context range. "
        f"{EXACT_SOURCE_DATA}{razor_instructions(relative_targets)}"
        f"{final_review}"
        "Check target-language constructs as well as source values: SQL placeholders such as :name are "
        "string binding keys, not valid C# property identifiers. Translate PHP string comparisons and null "
        "handling explicitly; copying the original operator spelling may be invalid or change behavior in C#. "
        "For database transactions, ensure each command participates in the active transaction as required by "
        "the target provider, including reads, and preserve commit/rollback behavior. "
        "Do not access files outside this workspace. Do not run builds, tests, or external checkers. "
        f"{STATIC_SOURCE_REVIEW}"
        "After verifying that all required changes for this capsule are saved, write exactly "
        f"`{completion_token}` as plain text to `{completion_path}` as your final file action. "
        "Leave this record absent while anything remains, including when blocked or at the step limit. "
        "The harness closes this session when it sees the record. Finish all edits and checks before writing it. "
        "If interrupted, preserve the edits and report what remains. "
        "Claim the entire migration is complete only after the final capsule."
    )
    return prompt, completion_path, completion_token, boundary_digest, verified_sources
