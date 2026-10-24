"""Apply available repository safeguards without changing visibility or billing."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

REPO = "3535602152/DouyinDownloader"


def request(path: str, method: str = "GET", data: dict | None = None) -> dict:
    args = ["gh", "api", path, "--method", method]
    if data is not None:
        args += ["--input", "-"]
    result = subprocess.run(args, input=json.dumps(data).encode() if data is not None else None,
                            capture_output=True)
    try:
        body = json.loads(result.stdout or b"{}")
    except ValueError:
        body = {}
    return {"ok": result.returncode == 0, "response": body,
            "message": result.stderr.decode("utf-8", "replace").strip()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-visibility", choices=("public", "private"), default="public",
                        help="Refuse to modify a repository with unexpected visibility")
    parser.add_argument("--output", type=Path, default=Path("build/repository-security.json"))
    args = parser.parse_args()
    repo = request(f"repos/{REPO}")
    if repo["response"].get("full_name") != REPO:
        raise SystemExit("Repository identity mismatch; refusing to modify settings")
    if repo["response"].get("visibility") != args.expected_visibility:
        raise SystemExit("Repository visibility mismatch; refusing to modify settings")
    results = {"repository": REPO, "repository_id": repo["response"]["id"],
               "visibility": args.expected_visibility}
    if args.apply:
        results["token_permissions"] = request(f"repos/{REPO}/actions/permissions/workflow", "PUT",
                                                {"default_workflow_permissions": "read",
                                                 "can_approve_pull_request_reviews": False})
        checks = [f"Python ({os_name}, {version})" for os_name in ("ubuntu-latest", "windows-latest")
                  for version in ("3.12", "3.13", "3.14")] + ["Frontend", "Security"]
        results["branch_protection"] = request(f"repos/{REPO}/branches/main/protection", "PUT",
            {"required_status_checks": {"strict": True, "contexts": checks}, "enforce_admins": True,
             "required_pull_request_reviews": {"required_approving_review_count": 1},
             "restrictions": None, "allow_force_pushes": False, "allow_deletions": False})
        results["private_reporting"] = request(f"repos/{REPO}/private-vulnerability-reporting", "PUT")
        results["dependabot_alerts"] = request(f"repos/{REPO}/vulnerability-alerts", "PUT")
        results["dependabot_security_updates"] = request(
            f"repos/{REPO}/automated-security-fixes", "PUT")
        results["secret_protection"] = request(f"repos/{REPO}", "PATCH",
            {"security_and_analysis": {"secret_scanning": {"status": "enabled"},
                                       "secret_scanning_push_protection": {"status": "enabled"}}})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print("Repository settings recorded:", args.output)
