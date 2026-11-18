"""Trusted orchestration. Run exclusively from the dispatch's main checkout."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


CHECKS = {
    f"regression ({os}-latest, {version})"
    for os in ("ubuntu", "windows")
    for version in ("3.11", "3.13")
} | {"windows-junie"}
CI_JOBS = CHECKS | {
    f"windows tests ({version}, {suite})"
    for version in ("3.11", "3.13")
    for suite in ("conversation-editor", "remaining")
}
CI = ".github/workflows/tests.yml"
ACTIONS_APP = 15368
CODEX_BOT = "chatgpt-codex-connector[bot]"
CODEX_BOT_ID = 199175422
RULES = Path(__file__).with_name("issue.md")


class Stop(Exception):
    """A missing prerequisite or unsafe state."""


def require(condition, message):
    if not condition:
        raise Stop(message)


def issue_number(value):
    require(re.fullmatch(r"[1-9][0-9]{0,9}", value) and int(value) <= 2147483647,
            "Enter one canonical positive Issue number.")
    return int(value)


def run(*args, data=None, error="Command failed."):
    result = subprocess.run(args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    require(result.returncode == 0, error)
    return result.stdout


def git(*args, **kwargs):
    return run("git", "-c", "core.hooksPath=/dev/null", *args, **kwargs)


def api(endpoint, *args, pages=False):
    resource = f"repos/{os.environ['GITHUB_REPOSITORY']}/{endpoint}"
    pagination = ("--paginate", "--slurp") if pages else ()
    return json.loads(run("gh", "api", resource, *pagination, *args,
                          error=f"GitHub API call failed: {endpoint.split('?')[0]}."))


def paginated(endpoint, field=None):
    return [item for page in api(endpoint, pages=True) for item in (page[field] if field else page)]


def output(name, value):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as file:
        file.write(f"{name}={value}\n")


def summary(message):
    print(message, flush=True)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as file:
        file.write(f"{message}\n\n")


def candidate_branch(number):
    return f"codex/issue-{number}"


def base():
    return os.environ["GITHUB_SHA"]


def fresh_main():
    require(api("git/ref/heads/main")["object"]["sha"] == base(),
            "main advanced. Start again from current main and revalidate.")


def snapshot(number):
    issue = api(f"issues/{number}")
    require(issue["state"] == "open" and "pull_request" not in issue,
            "The number must identify an open Issue in this repository.")
    comments = paginated(f"issues/{number}/comments?per_page=100")
    return json.dumps({
        "number": number, "title": issue["title"], "body": issue["body"],
        "comments": [
            {key: comment[key] for key in ("id", "body", "created_at", "updated_at")}
            | {"author": comment["user"]["login"]}
            for comment in comments
        ],
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unchanged_issue(number):
    require(digest(snapshot(number)) == os.environ["SNAPSHOT"],
            "Issue or discussion changed. Review the current requirements and rerun.")


def no_duplicate(number):
    branch = candidate_branch(number)
    owner = os.environ["GITHUB_REPOSITORY_OWNER"]
    require(not api(f"pulls?state=open&head={owner}:{branch}"),
            f"An open candidate PR already uses {branch}.")
    refs = api(f"git/matching-refs/heads/{branch}")
    require(all(ref["ref"] != f"refs/heads/{branch}" for ref in refs),
            f"Candidate branch {branch} already exists. Check for a PR or a previous incomplete run.")


def allowed_dispatcher(actor, allowlist):
    logins = {login.strip().lower() for login in allowlist.split(",")}
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,38}", actor.lower())) and actor.lower() in logins


def intake(number):
    require(allowed_dispatcher(os.environ["GITHUB_ACTOR"], os.environ["DISPATCHERS"]),
            "Only maintainers listed in CODEX_ISSUE_DISPATCHERS may run generation.")
    fresh_main()
    no_duplicate(number)
    data = snapshot(number)
    require(len(data) <= 128_000, "Issue discussion exceeds the 128 KB intake limit.")
    output("snapshot", digest(data))
    (Path(os.environ["RUNNER_TEMP"]) / "issue-prompt.txt").write_bytes(RULES.read_bytes() + data)


def allowed_path(path):
    # Portable names prevent Windows aliasing, ADS, and Git control paths.
    if not re.fullmatch(
        r"src/migration_harness/(?:[a-zA-Z0-9_-]+/)*[a-zA-Z0-9_-]+\.(?:py|js)"
        r"|tests/test_[a-zA-Z0-9_-]+\.py", path
    ):
        return False
    return not any(re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])", part.split(".")[0])
                   for part in path.split("/"))


def validate_patch(patch):
    require(patch.startswith(b"diff --git ") and b"\0" not in patch and len(patch) <= 200_000,
            "Proposal must be a nonempty text Git diff of at most 200 KB.")
    require(git("diff", "--cached", "--name-only") == b"", "Expected a clean index.")
    git("apply", "--cached", "--whitespace=error", "-", data=patch,
        error="Patch does not apply cleanly to the trusted base.")
    changes = git("diff", "--cached", "--raw", "--no-renames", "--no-abbrev", "-z").split(b"\0")[:-1]
    require(changes, "Patch did not change any files.")
    for meta, raw_path in zip(changes[::2], changes[1::2], strict=True):
        old_mode, new_mode, _, blob, status = meta.removeprefix(b":").split()
        require(status in {b"A", b"M", b"D"} and {old_mode, new_mode} <= {b"000000", b"100644"}
                and allowed_path(raw_path.decode("utf-8")),
                "Patch changes a restricted path or file mode.")
        if new_mode != b"000000":
            content = git("cat-file", "blob", blob.decode("ascii"))
            require(b"\0" not in content, "Patch adds binary content.")
            content.decode("utf-8")
    entries = {}
    for path in git("ls-files", "-z").decode("utf-8").split("\0")[:-1]:
        parts = path.split("/")
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            key = prefix.casefold()
            entry = (prefix, length == len(parts))
            require(entries.setdefault(key, entry) == entry,
                    "The resulting tree has case-insensitive path collisions.")


def proposal_bytes(path):
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 512_000,
            "Missing or oversized proposal artifact.")
    return path.read_bytes()


def seal_proposal(_number):
    proposal = Path(os.environ["RUNNER_TEMP"]) / "proposal.json"
    output("digest", digest(proposal_bytes(proposal)))


def commit_proposal(number):
    proposal = Path(os.environ["RUNNER_TEMP"]) / "proposal" / "proposal.json"
    raw = proposal_bytes(proposal)
    require(digest(raw) == os.environ["PROPOSAL_DIGEST"], "Proposal artifact digest mismatch.")
    data = json.loads(raw)
    require(isinstance(data, dict) and set(data) == {"patch", "report"}
            and all(isinstance(item, str) for item in data.values()), "Invalid proposal schema.")
    require(data["patch"], "Codex did not propose a patch. See proposal.json for investigation and next steps.")
    require(git("rev-parse", "HEAD").strip().decode("ascii") == base(), "Unexpected checkout revision.")
    validate_patch(data["patch"].encode("utf-8"))
    git("-c", "user.name=Codex Issue Automation", "-c", "user.email=codex-issue@users.noreply.github.com",
        "-c", "commit.gpgsign=false", "commit", "-m", f"Address issue #{number}")
    output("head", git("rev-parse", "HEAD").strip().decode("ascii"))


def run_url():
    return f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"


def pr_body(number, head):
    repo = os.environ["GITHUB_REPOSITORY"]
    return (
        f"Refs #{number}\n\n"
        f"Codex investigated the issue and proposed this patch for `{head}`. "
        f"The investigation, planned tests and limitations are in `proposal.json` in the "
        f"[dispatch run artifacts]({run_url()}).\n\n"
        "Verification is tracked in the PR Checks and the corresponding branch push run "
        "(Windows and Ubuntu, Python 3.11/3.13 and Windows Junie startup smoke).\n\n"
        "For Codex GitHub review, mark this draft ready and request `@codex review` if needed. "
        f"Follow the [operator guide](https://github.com/{repo}/blob/main/docs/codex-issue-workflow.md) to record a "
        "maintainer approval and any authenticated Junie evidence. The Issue stays open for maintainer follow-up."
    )


def publish(number):
    branch = candidate_branch(number)
    ref = f"refs/heads/{branch}"
    git("-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
        "push", f"--force-with-lease={ref}:", "origin", f"HEAD:{ref}",
        error=f"Could not create {branch}. Check the App permissions and candidate branch.")
    head = git("rev-parse", "HEAD").strip().decode("ascii")
    pr = api("pulls", "-X", "POST", "-f", f"head={branch}", "-f", "base=main",
             "-f", f"title=Codex candidate for issue #{number}", "-F", "draft=true",
             "-f", f"body={pr_body(number, head)}")
    output("pr", pr["number"])
    summary(f"Created draft PR: {pr['html_url']} (head `{head}`). Mark ready to request Codex review.")


def rules_ready():
    rules = paginated("rules/branches/main?per_page=100")
    checks = any(
        rule["type"] == "required_status_checks"
        and rule["parameters"]["strict_required_status_checks_policy"]
        and CHECKS <= {check["context"] for check in rule["parameters"]["required_status_checks"]
                       if check.get("integration_id") == ACTIONS_APP}
        for rule in rules
    )
    approvals = any(rule["type"] == "pull_request"
                    and rule["parameters"]["required_approving_review_count"] >= 1
                    and rule["parameters"]["dismiss_stale_reviews_on_push"]
                    and rule["parameters"]["required_review_thread_resolution"]
                    for rule in rules)
    require(checks and approvals and not any(rule["type"] == "merge_queue" for rule in rules),
            "main needs the documented active strict ruleset and no merge queue.")


def ci_ready(branch, head):
    runs = paginated(f"actions/workflows/tests.yml/runs?head_sha={head}&branch={branch}&per_page=100",
                     "workflow_runs")
    waiting = []
    for event in ("push", "pull_request"):
        matches = [run for run in runs if run["event"] == event and run["head_sha"] == head
                   and run["head_branch"] == branch and run["path"] == CI
                   and run["head_repository"]["full_name"] == os.environ["GITHUB_REPOSITORY"]]
        if not matches:
            waiting.append(f"missing {event} CI")
            continue
        latest = max(matches, key=lambda run: run["id"])
        if latest["status"] != "completed":
            waiting.append(f"{event} CI pending")
            continue
        require(latest["conclusion"] == "success", f"{event} CI {latest['conclusion']}. See run {latest['id']}.")
        jobs = paginated(f"actions/runs/{latest['id']}/attempts/{latest['run_attempt']}/jobs?per_page=100", "jobs")
        require(len(jobs) == len(CI_JOBS) and {job["name"] for job in jobs} == CI_JOBS
                and all(job["head_sha"] == head and job["status"] == "completed"
                        and job["conclusion"] == "success" for job in jobs),
                f"{event} CI lacks the full successful matrix, shards, and smoke at the expected SHA.")
    return waiting


def unique_json_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate fields in Codex review state.")
    return result


def clean_review(comment, head):
    body = comment["body"]
    marker = "<!-- codex-review-state:"
    if marker in body:
        if body.count(marker) != 1 or not body.startswith("<!-- codex-pull-request-review-summary -->\n"):
            return False
        match = re.search(r"^<!-- codex-review-state:v1 (.+) -->$", body, flags=re.MULTILINE)
        if not match:
            return False
        try:
            state = json.loads(match[1], object_pairs_hook=unique_json_object)
        except ValueError:
            return False
        review = state.get("code_review") if isinstance(state, dict) else None
        return (
            isinstance(review, dict)
            and review.get("commit_sha") == head
            and review.get("status") == "completed"
            and review.get("has_any_findings") is False
            and review.get("has_unresolved_findings") is False
        )
    matches = re.findall(r"(?:^Reviewed commit:|^\*\*Reviewed commit:\*\*) `([0-9a-f]{10,40})`$",
                         body, flags=re.MULTILINE)
    return body.startswith("Codex Review: Didn't find any major issues.") and bool(
        len(matches) == 1 and head.startswith(matches[0])
    )


def review_ready(pr, head):
    reviewers = {login.strip().lower() for login in os.environ["REVIEWERS"].split(",") if login.strip()}
    require(reviewers, "Configure CODEX_ISSUE_REVIEWERS before enabling merges.")
    comments = paginated(f"issues/{pr}/comments?per_page=100")
    bot = [comment for comment in comments if comment["user"]["login"] == CODEX_BOT
           and comment["user"]["type"] == "Bot" and comment["user"]["id"] == CODEX_BOT_ID]
    if not bot or not clean_review(bot[-1], head):
        return "Waiting for a supported clean Codex GitHub review of the exact head."
    reviews = paginated(f"pulls/{pr}/reviews?per_page=100")
    if any(review["user"]["id"] == CODEX_BOT_ID and review["submitted_at"]
           and review["submitted_at"] >= bot[-1]["updated_at"] for review in reviews):
        return "Codex posted another review after the clean verdict. Confirm the final result."
    decisions = {}
    for review in reviews:
        login = review["user"]["login"].lower()
        if (login in reviewers and review["user"]["type"] == "User"
                and review["author_association"] in {"OWNER", "MEMBER", "COLLABORATOR"}
                and review["state"] not in {"COMMENTED", "PENDING"}):
            decisions[login] = review
    if any(review["state"] == "CHANGES_REQUESTED" for review in decisions.values()):
        return "An authorized maintainer requested changes."
    for review in decisions.values():
        if (review["state"] == "APPROVED" and review["commit_id"] == head
                and review["submitted_at"] >= bot[-1]["updated_at"]
                and re.fullmatch(r"Codex-Issue: verified\s+Junie: (?:not-required|passed)",
                                 review["body"].strip())):
            return None
    return "Waiting for maintainer approval confirming Issue completion and Junie requirements."


def current_pr(number, pr, head):
    fresh_main()
    value = api(f"pulls/{pr}")
    require(value["state"] == "open" and value["head"]["sha"] == head
            and value["head"]["ref"] == candidate_branch(number)
            and value["base"]["ref"] == "main" and value["base"]["sha"] == base()
            and value["head"]["repo"]["full_name"] == value["base"]["repo"]["full_name"]
            == os.environ["GITHUB_REPOSITORY"] and value["body"] == pr_body(number, head),
            "Candidate PR changed. Check the head, base, description and PR state.")
    require(value["mergeable"] is not False, "Candidate has a merge conflict.")
    return value


def validate_merge(number, wait):
    head = os.environ["HEAD"]
    pr = issue_number(os.environ["PR"])
    require(re.fullmatch(r"[0-9a-f]{40}", head), "Invalid expected candidate SHA.")
    rules_ready()
    deadline = time.monotonic() + (3300 if wait else 0)
    while True:
        unchanged_issue(number)
        value = current_pr(number, pr, head)
        pending = ci_ready(candidate_branch(number), head)
        review = review_ready(pr, head)
        if review:
            pending.append(review)
        if value["draft"] or value["mergeable"] is not True or value["mergeable_state"] != "clean":
            pending.append("PR must be ready and mergeable under GitHub's rules")
        if not pending:
            summary(f"Verified both CI events, Codex review and maintainer approval for PR #{pr} at `{head}`.")
            return
        message = " / ".join(pending)
        require(time.monotonic() < deadline, message)
        print(message, flush=True)
        time.sleep(30)


def merge(number):
    pr = issue_number(os.environ["PR"])
    head = os.environ["HEAD"]
    rules_ready()
    value = current_pr(number, pr, head)
    require(not value["draft"] and value["mergeable"] is True and value["mergeable_state"] == "clean",
            "GitHub no longer reports a clean, ready PR.")
    result = api(f"pulls/{pr}/merge", "-X", "PUT", "-f", f"sha={head}", "-f", "merge_method=squash",
                 "-f", f"commit_title=Address issue #{number} (#{pr})",
                 "-f", f"commit_message=Refs #{number}")
    require(result["merged"], "GitHub did not merge the PR. Check its merge requirements.")
    summary(f"Merged PR #{pr} as `{result['sha']}`. Issue #{number} remains open for maintainer follow-up.")


def prepublish(number):
    fresh_main()
    unchanged_issue(number)
    no_duplicate(number)


def main():
    commands = {
        "intake": intake,
        "seal": seal_proposal,
        "patch": commit_proposal,
        "prepublish": prepublish,
        "publish": publish,
        "verify": lambda number: validate_merge(number, wait=True),
        "gate": lambda number: validate_merge(number, wait=False),
        "merge": merge,
    }
    try:
        commands[sys.argv[1]](issue_number(os.environ["ISSUE"]))
    except Stop as exc:
        summary(f"Stopped: {exc}")
        return 1
    except (KeyError, TypeError, ValueError, OSError):
        summary("Stopped: missing configuration, unexpected API data, or invalid proposal.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
