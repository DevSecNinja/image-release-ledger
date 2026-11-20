"""Tests for scripts/ledger.py. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location(
    "ledger", Path(__file__).resolve().parent.parent / "scripts" / "ledger.py"
)
ledger = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ledger)

NOW = "2026-11-20T08:00:00Z"
REG = "https://ghcr.io/v2/example/app"
API = "https://api.github.com"
CHALLENGE = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:example/app:pull"'


def digest(n: int) -> str:
    return "sha256:" + f"{n:x}".rjust(64, "0")


class Fake:
    """A transport answering from a route table; records every request."""

    def __init__(self, routes: dict, token_required: bool = True):
        self.routes = routes
        self.token_required = token_required
        self.calls = []

    def __call__(self, method, url, headers, data):
        self.calls.append((method, url, headers, data))
        if url.startswith("https://ghcr.io/token"):
            return 200, {}, b'{"token": "anon"}'
        if url.startswith("https://ghcr.io/") and self.token_required and headers.get("Authorization") != "Bearer anon":
            return 401, {"www-authenticate": CHALLENGE}, b""
        route = self.routes.get((method, url))
        if route is None:
            return 404, {}, b'{"message": "Not Found"}'
        return route(data) if callable(route) else route

    def heads(self):
        return [url for method, url, _, _ in self.calls if method == "HEAD"]


def tags_route(tags, link=None):
    return 200, ({"link": link} if link else {}), json.dumps({"tags": tags}).encode()


def manifest(n):
    return 200, {"docker-content-digest": digest(n)}, b""


def file_route(content: dict):
    return 200, {}, json.dumps({"content": base64.b64encode(json.dumps(content).encode()).decode()}).encode()


class ParseImagesTest(unittest.TestCase):
    def test_parses_names_options_and_comments(self):
        images = ledger.parse_images(
            "# comment\n\nghcr.io/example/app  seed=github-releases:example/app  # trailing\nquay.io/a/b\n"
        )
        self.assertEqual([i["name"] for i in images], ["ghcr.io/example/app", "quay.io/a/b"])
        self.assertEqual(images[0]["host"], "ghcr.io")
        self.assertEqual(images[0]["repo"], "example/app")
        self.assertEqual(images[0]["seed"], "example/app")
        self.assertIsNone(images[1]["seed"])

    def test_rejects_docker_hub_missing_host_traversal_and_unknown_options(self):
        for line in ["docker.io/library/nginx", "nginx", "library/nginx", "ghcr.io/a/../b", "ghcr.io/a/b foo=bar"]:
            with self.subTest(line=line), self.assertRaises(ledger.LedgerError):
                ledger.parse_images(line)


class RegistryTest(unittest.TestCase):
    def test_fetches_an_anonymous_token_once_and_follows_pages(self):
        fake = Fake(
            {
                ("GET", f"{REG}/tags/list?n=1000"): tags_route(
                    ["1.0.0"], '</v2/example/app/tags/list?last=1.0.0&n=1000>; rel="next"'
                ),
                ("GET", f"{REG}/tags/list?last=1.0.0&n=1000"): tags_route(["1.1.0"]),
                ("HEAD", f"{REG}/manifests/1.1.0"): manifest(7),
            }
        )
        registry = ledger.Registry(fake, "ghcr.io")
        self.assertEqual(registry.tags("example/app"), ["1.0.0", "1.1.0"])
        self.assertEqual(registry.digest("example/app", "1.1.0"), digest(7))
        self.assertEqual(sum(url.startswith("https://ghcr.io/token") for _, url, _, _ in fake.calls), 1)
        head = [c for c in fake.calls if c[0] == "HEAD"][-1]
        self.assertIn("application/vnd.oci.image.index.v1+json", head[2]["Accept"])

    def test_errors_without_a_digest(self):
        fake = Fake({("HEAD", f"{REG}/manifests/1.0.0"): (200, {}, b"")})
        with self.assertRaises(ledger.LedgerError):
            ledger.Registry(fake, "ghcr.io").digest("example/app", "1.0.0")


def update(*args, **kwargs):
    """update_image's ledger only."""
    return ledger.update_image(*args, **kwargs)[0]


def release(published_at, immutable=None):
    return {"published_at": published_at, "immutable": immutable}


class UpdateImageTest(unittest.TestCase):
    def test_new_image_records_now_and_each_digest(self):
        result = update(None, ["1.0.0", "1.1.0"], {"1.0.0": digest(1), "1.1.0": digest(2)}.get, NOW)
        self.assertEqual(
            result["releases"],
            [
                {"version": "1.0.0", "releaseTimestamp": NOW, "digest": digest(1)},
                {"version": "1.1.0", "releaseTimestamp": NOW, "digest": digest(2)},
            ],
        )

    def test_seed_dates_apply_only_to_a_new_image_and_never_lie_in_the_future(self):
        info = {"1.0.0": release("2026-10-01T00:00:00Z"), "1.1.0": release("2026-12-01T00:00:00Z")}
        result = update(None, ["1.0.0", "1.1.0"], lambda t: digest(1), NOW, info)
        by_tag = {r["version"]: r for r in result["releases"]}
        self.assertEqual(by_tag["1.0.0"]["releaseTimestamp"], "2026-10-01T00:00:00Z")
        self.assertEqual(by_tag["1.0.0"]["seed"], "github-release")
        self.assertEqual(by_tag["1.1.0"]["releaseTimestamp"], NOW)
        self.assertNotIn("seed", by_tag["1.1.0"])

        existing = {"releases": [{"version": "1.2.0", "releaseTimestamp": NOW, "digest": digest(1)}]}
        later = update(existing, ["1.2.0", "1.0.0"], lambda t: digest(2), "2026-11-21T00:00:00Z", info)
        self.assertEqual(later["releases"][1]["releaseTimestamp"], "2026-11-21T00:00:00Z")
        self.assertNotIn("seed", later["releases"][1])


class FloatingTagTest(unittest.TestCase):
    OLD = "2026-10-01T00:00:00Z"

    def test_a_floating_tag_inherits_the_first_sight_of_its_digest(self):
        info = {"1.0.0": release(self.OLD)}
        result = update(None, ["1.0.0", "latest", "1"], lambda t: digest(1), NOW, info)
        by_tag = {r["version"]: r for r in result["releases"]}
        self.assertEqual(by_tag["latest"], {"version": "latest", "releaseTimestamp": self.OLD, "digest": digest(1)})
        self.assertEqual(result["floating"]["1"], [{"digest": digest(1), "seen": self.OLD}])

    def test_an_unsoaked_floating_tag_is_left_out(self):
        result = update(None, ["beta"], lambda t: digest(5), NOW)
        self.assertEqual(result["releases"], [])
        self.assertEqual(result["floating"], {"beta": [{"digest": digest(5), "seen": NOW}]})

    def test_a_move_keeps_exposing_the_newest_soaked_digest_until_the_new_one_soaks(self):
        existing = {"releases": [], "floating": {"latest": [{"digest": digest(1), "seen": self.OLD}]}}
        moved, events = ledger.update_image(existing, ["latest"], lambda t: digest(2), NOW)
        self.assertEqual(events, [])
        self.assertEqual(moved["releases"][0]["digest"], digest(1))
        self.assertEqual(moved["floating"]["latest"][-1], {"digest": digest(2), "seen": NOW})

        later = "2026-12-04T08:00:00Z"
        soaked = update(moved, ["latest"], lambda t: digest(2), later)
        self.assertEqual(soaked["releases"][0], {"version": "latest", "releaseTimestamp": NOW, "digest": digest(2)})

    def test_moving_back_to_a_known_digest_keeps_its_original_sight(self):
        history = [{"digest": digest(1), "seen": self.OLD}, {"digest": digest(2), "seen": NOW}]
        result = update({"releases": [], "floating": {"latest": history}}, ["latest"], lambda t: digest(1), NOW)
        self.assertEqual(result["floating"]["latest"][-1], {"digest": digest(1), "seen": self.OLD})
        self.assertEqual(result["releases"][0]["digest"], digest(1))

    def test_the_soak_window_is_configurable(self):
        result = update(None, ["beta"], lambda t: digest(5), NOW, soak_days=0)
        self.assertEqual(result["releases"][0]["digest"], digest(5))
        (image,) = ledger.parse_images("ghcr.io/a/b soak-days=7\n")
        self.assertEqual(image["soak_days"], 7)
        for bad in ("soak-days=-1", "soak-days=7d", "soak-days=1000"):
            with self.subTest(bad), self.assertRaises(ledger.LedgerError):
                ledger.parse_images(f"ghcr.io/a/b {bad}\n")

    def test_a_retagged_digest_counts_as_seen_when_the_tag_moved(self):
        moved_at = "2026-10-05T00:00:00Z"
        exact = {"version": "1.0.0", "releaseTimestamp": self.OLD, "digest": digest(1)}
        exact["retagged"] = [{"from": digest(1), "to": digest(2), "seen": moved_at}]
        result = update({"releases": [exact]}, ["1.0.0", "latest"], lambda t: digest(2), NOW)
        self.assertEqual(result["floating"]["latest"], [{"digest": digest(2), "seen": moved_at}])
        self.assertEqual(result["releases"][0]["digest"], digest(1))

    def test_a_floating_tag_that_disappears_stays_visible_as_deprecated(self):
        existing = {"releases": [], "floating": {"latest": [{"digest": digest(1), "seen": self.OLD}]}}
        gone = update(existing, [], lambda t: digest(9), NOW)
        self.assertEqual(
            gone["releases"],
            [{"version": "latest", "releaseTimestamp": self.OLD, "digest": digest(1), "isDeprecated": True}],
        )
        back = update(gone, ["latest"], lambda t: digest(1), NOW)
        self.assertNotIn("isDeprecated", back["releases"][0])

    def test_legacy_floating_entries_move_into_the_history(self):
        existing = {"releases": [{"version": "latest", "releaseTimestamp": self.OLD, "digest": digest(1)}]}
        result = update(existing, ["latest"], lambda t: digest(1), NOW)
        self.assertEqual(result["floating"], {"latest": [{"digest": digest(1), "seen": self.OLD}]})
        self.assertEqual(result["releases"][0]["releaseTimestamp"], self.OLD)

    def test_a_legacy_entry_takes_the_earlier_sight_of_the_same_digest_under_a_version_tag(self):
        existing = {
            "releases": [
                {"version": "1.0.0", "releaseTimestamp": self.OLD, "digest": digest(1)},
                {"version": "latest", "releaseTimestamp": NOW, "digest": digest(1)},
            ]
        }
        result = update(existing, ["1.0.0", "latest"], lambda t: digest(1), NOW)
        self.assertEqual(result["floating"]["latest"], [{"digest": digest(1), "seen": self.OLD}])
        self.assertIn({"version": "latest", "releaseTimestamp": self.OLD, "digest": digest(1)}, result["releases"])


class MoreUpdateImageTest(unittest.TestCase):
    def test_new_tags_record_the_release_immutable_flag(self):
        info = {"1.0.0": release("2026-10-01T00:00:00Z", True), "1.1.0": release("2026-10-02T00:00:00Z", False)}
        existing = {"releases": []}
        result = update(existing, ["1.0.0", "1.1.0", "1.2.0"], lambda t: digest(1), NOW, info)
        by_tag = {r["version"]: r for r in result["releases"]}
        self.assertIs(by_tag["1.0.0"]["immutable"], True)
        self.assertIs(by_tag["1.1.0"]["immutable"], False)
        self.assertNotIn("immutable", by_tag["1.2.0"])
        self.assertEqual(by_tag["1.0.0"]["releaseTimestamp"], NOW)

    def test_a_moved_version_tag_keeps_its_first_digest_and_is_reported_once(self):
        first = {"version": "1.0.0", "releaseTimestamp": "2026-10-01T00:00:00Z", "digest": digest(1)}
        moved, events = ledger.update_image({"releases": [first]}, ["1.0.0"], lambda t: digest(2), NOW)
        (entry,) = moved["releases"]
        self.assertEqual((entry["digest"], entry["releaseTimestamp"]), (digest(1), "2026-10-01T00:00:00Z"))
        self.assertEqual(entry["retagged"], [{"from": digest(1), "to": digest(2), "seen": NOW}])
        self.assertEqual(events, [{"tag": "1.0.0", "from": digest(1), "to": digest(2), "seen": NOW}])

        again, events = ledger.update_image(moved, ["1.0.0"], lambda t: digest(2), "2026-11-21T00:00:00Z")
        self.assertEqual((again, events), (moved, []))

        back, events = ledger.update_image(moved, ["1.0.0"], lambda t: digest(1), "2026-11-22T00:00:00Z")
        self.assertEqual(len(back["releases"][0]["retagged"]), 2)
        self.assertEqual(events[0]["to"], digest(1))

    def test_signature_and_attestation_tags_are_left_out(self):
        sig = "sha256-" + "a" * 64
        existing = {"releases": [{"version": f"{sig}.sig", "releaseTimestamp": NOW, "digest": digest(1)}]}
        result = update(existing, ["1.0.0", sig, f"{sig}.sig", f"{sig}.att"], lambda t: digest(2), NOW)
        self.assertEqual([r["version"] for r in result["releases"]], ["1.0.0"])

    def test_removed_tags_are_kept_as_deprecated_until_they_return(self):
        existing = {"releases": [{"version": "1.0.0", "releaseTimestamp": NOW, "digest": digest(1)}]}
        gone = update(existing, [], lambda t: digest(2), NOW)
        self.assertTrue(gone["releases"][0]["isDeprecated"])
        back = update(gone, ["1.0.0"], lambda t: digest(1), NOW)
        self.assertNotIn("isDeprecated", back["releases"][0])
        self.assertEqual(back["releases"][0]["digest"], digest(1))

    def test_source_url_is_kept(self):
        result = update(None, [], lambda t: digest(1), NOW, source_url="https://github.com/example/app")
        self.assertEqual(list(result), ["sourceUrl", "releases"])


class RunTest(unittest.TestCase):
    IMAGES = "ghcr.io/example/app seed=github-releases:example/app\nghcr.io/example/broken\n"

    def routes(self, existing=None, branch_exists=True):
        commits = []
        routes = {
            ("GET", f"{REG}/tags/list?n=1000"): tags_route(["1.0.0", "1.1.0"]),
            ("HEAD", f"{REG}/manifests/1.0.0"): manifest(1),
            ("HEAD", f"{REG}/manifests/1.1.0"): manifest(2),
            ("GET", "https://ghcr.io/v2/example/broken/tags/list?n=1000"): (500, {}, b""),
            ("GET", f"{API}/repos/example/app/releases?per_page=100&page=1"): (
                200,
                {},
                json.dumps([{"tag_name": "v1.0.0", "published_at": "2026-10-01T00:00:00Z"}]).encode(),
            ),
            ("GET", f"{API}/repos/DevSecNinja/ledger"): (200, {}, b'{"default_branch": "main"}'),
            ("GET", f"{API}/repos/DevSecNinja/ledger/git/ref/heads/main"): (200, {}, b'{"object": {"sha": "m1"}}'),
            ("POST", f"{API}/repos/DevSecNinja/ledger/git/refs"): (201, {}, b"{}"),
            ("POST", f"{API}/graphql"): lambda data: (
                commits.append(json.loads(data))
                or (200, {}, b'{"data": {"createCommitOnBranch": {"commit": {"oid": "c2"}}}}')
            ),
        }
        if branch_exists:
            routes[("GET", f"{API}/repos/DevSecNinja/ledger/git/ref/heads/data")] = (
                200,
                {},
                b'{"object": {"sha": "d1"}}',
            )
        if existing is not None:
            path = "images/ghcr.io/example/app.json"
            routes[("GET", f"{API}/repos/DevSecNinja/ledger/contents/{path}?ref=data")] = file_route(existing)
        return routes, commits

    def run_ledger(self, routes, apply=True, images=None):
        fake = Fake(routes)
        github = ledger.GitHub(fake, "token", "DevSecNinja/ledger")
        with redirect_stdout(io.StringIO()) as out:
            code = ledger.run(ledger.parse_images(images or self.IMAGES), github, fake, "data", NOW, apply)
        return code, fake, out.getvalue()

    def test_a_network_error_skips_only_that_image(self):
        routes, commits = self.routes()

        def unreachable(_data):
            raise urllib.error.URLError("name resolution failed")

        routes[("GET", "https://ghcr.io/v2/example/down/tags/list?n=1000")] = unreachable
        code, _fake, out = self.run_ledger(
            routes, images="ghcr.io/example/down\nghcr.io/example/app seed=github-releases:example/app\n"
        )
        self.assertEqual(code, 1)
        self.assertIn("::error::ghcr.io/example/down", out)
        (addition,) = commits[0]["variables"]["input"]["fileChanges"]["additions"]
        self.assertEqual(addition["path"], "images/ghcr.io/example/app.json")

    def test_commits_good_images_signed_on_the_data_branch_and_fails_for_broken_ones(self):
        routes, commits = self.routes()
        code, _fake, out = self.run_ledger(routes)
        self.assertEqual(code, 1)
        self.assertIn("::error::ghcr.io/example/broken", out)
        (mutation,) = commits
        payload = mutation["variables"]["input"]
        self.assertEqual(payload["branch"]["branchName"], "data")
        self.assertEqual(payload["expectedHeadOid"], "d1")
        (addition,) = payload["fileChanges"]["additions"]
        self.assertEqual(addition["path"], "images/ghcr.io/example/app.json")
        written = json.loads(base64.b64decode(addition["contents"]))
        self.assertEqual(written["sourceUrl"], "https://github.com/example/app")
        self.assertEqual(
            [(r["version"], r["releaseTimestamp"], r.get("seed")) for r in written["releases"]],
            [("1.0.0", "2026-10-01T00:00:00Z", "github-release"), ("1.1.0", NOW, None)],
        )

    def test_creates_the_data_branch_from_main_when_missing(self):
        routes, commits = self.routes(branch_exists=False)
        _, fake, _ = self.run_ledger(routes)
        create = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/git/refs")]
        self.assertEqual(json.loads(create[0][3]), {"ref": "refs/heads/data", "sha": "m1"})
        self.assertEqual(commits[0]["variables"]["input"]["expectedHeadOid"], "m1")

    def existing(self, first_digest=None):
        return {
            "sourceUrl": "https://github.com/example/app",
            "releases": [
                {"version": "1.0.0", "releaseTimestamp": "2026-10-01T00:00:00Z", "digest": first_digest or digest(1)},
                {"version": "1.1.0", "releaseTimestamp": NOW, "digest": digest(2)},
            ],
        }

    def test_no_new_tags_means_no_commit_and_no_release_reads(self):
        routes, commits = self.routes(existing=self.existing())
        _, fake, out = self.run_ledger(routes, images="ghcr.io/example/app seed=github-releases:example/app\n")
        self.assertEqual(commits, [])
        self.assertEqual(sorted(fake.heads()), [f"{REG}/manifests/1.0.0", f"{REG}/manifests/1.1.0"])
        self.assertFalse([c for c in fake.calls if "/releases" in c[1]])
        self.assertIn("No new tags.", out)

    def issue_routes(self, routes, open_issues=(), status=201, second_page=()):
        issues = []
        listing = f"{API}/repos/DevSecNinja/ledger/issues?state=open&per_page=100&page="
        routes[("GET", f"{listing}1")] = (200, {}, json.dumps(list(open_issues)).encode())
        routes[("GET", f"{listing}2")] = (200, {}, json.dumps(list(second_page)).encode())
        routes[("POST", f"{API}/repos/DevSecNinja/ledger/issues/1/comments")] = lambda data: (
            self.comments.append(json.loads(data)) or (201, {}, b"{}")
        )
        routes[("POST", f"{API}/repos/DevSecNinja/ledger/issues")] = lambda data: (
            issues.append(json.loads(data)) or (status, {}, b'{"html_url": "https://x/issues/9"}')
        )
        return issues

    def written(self, commits):
        (addition,) = commits[0]["variables"]["input"]["fileChanges"]["additions"]
        return json.loads(base64.b64decode(addition["contents"]))

    def test_a_moved_tag_is_committed_summarised_and_opens_one_issue(self):
        routes, commits = self.routes(existing=self.existing(first_digest=digest(7)))
        issues = self.issue_routes(routes)
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.md"
            with mock.patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(summary)}):
                code, _fake, out = self.run_ledger(
                    routes, images="ghcr.io/example/app seed=github-releases:example/app\n"
                )
            text = summary.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("::warning::ghcr.io/example/app:1.0.0 moved", out)
        entry = self.written(commits)["releases"][0]
        self.assertEqual(entry["digest"], digest(7))
        self.assertEqual(
            entry["retagged"], [{"from": digest(7), "to": digest(1), "seen": NOW, "issue": "https://x/issues/9"}]
        )
        self.assertIn("`ghcr.io/example/app:1.0.0`", text)
        (issue,) = issues
        self.assertEqual(issue["title"], "Tag moved: ghcr.io/example/app:1.0.0")
        self.assertIn(f"First-seen digest (Renovate keeps using it): `{digest(7)}`", issue["body"])

    def test_a_failed_issue_is_committed_unnotified_and_retried_next_run(self):
        routes, commits = self.routes(existing=self.existing(first_digest=digest(7)))
        self.issue_routes(routes, status=403)
        code, _fake, out = self.run_ledger(routes, images="ghcr.io/example/app\n")
        self.assertEqual((code, len(commits)), (1, 1))
        self.assertIn("::error::could not open an issue", out)
        pending = self.written(commits)

        routes, commits = self.routes(existing=pending)
        issues = self.issue_routes(routes)
        code, _fake, _out = self.run_ledger(routes, images="ghcr.io/example/app\n")
        self.assertEqual((code, len(issues)), (0, 1))
        self.assertEqual(self.written(commits)["releases"][0]["retagged"][0]["issue"], "https://x/issues/9")

    def setUp(self):
        self.comments = []

    def test_a_second_move_names_the_first_seen_digest_and_comments_on_the_open_issue(self):
        moved = self.existing(first_digest=digest(7))
        issue = "https://x/issues/1"
        moved["releases"][0]["retagged"] = [{"from": digest(7), "to": digest(8), "seen": NOW, "issue": issue}]
        routes, commits = self.routes(existing=moved)
        title = "Tag moved: ghcr.io/example/app:1.0.0"
        issues = self.issue_routes(routes, open_issues=[{"title": title, "html_url": issue}])
        code, _fake, _out = self.run_ledger(routes, images="ghcr.io/example/app\n")
        self.assertEqual((code, issues), (0, []))
        second = self.written(commits)["releases"][0]["retagged"][1]
        self.assertEqual((second["from"], second["to"], second["issue"]), (digest(8), digest(1), issue))
        (comment,) = self.comments
        self.assertIn(f"Previous digest: `{digest(8)}`", comment["body"])
        self.assertIn(f"First-seen digest (Renovate keeps using it): `{digest(7)}`", comment["body"])

    def test_an_open_issue_on_a_later_page_is_found(self):
        routes, commits = self.routes(existing=self.existing(first_digest=digest(7)))
        title = "Tag moved: ghcr.io/example/app:1.0.0"
        others = [{"title": f"other {n}", "html_url": f"https://x/issues/{n + 100}"} for n in range(100)]
        issues = self.issue_routes(
            routes, open_issues=others, second_page=[{"title": title, "html_url": "https://x/issues/1"}]
        )
        code, _fake, _out = self.run_ledger(routes, images="ghcr.io/example/app\n")
        self.assertEqual((code, issues, len(self.comments)), (0, [], 1))
        self.assertEqual(self.written(commits)["releases"][0]["retagged"][0]["issue"], "https://x/issues/1")

    def test_dry_run_writes_nothing(self):
        routes, commits = self.routes()
        _, fake, out = self.run_ledger(routes, apply=False)
        self.assertEqual(commits, [])
        self.assertFalse([c for c in fake.calls if c[0] == "POST"])
        self.assertIn("would write images/ghcr.io/example/app.json (2 releases)", out)


if __name__ == "__main__":
    unittest.main()
