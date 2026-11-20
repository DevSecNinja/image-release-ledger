"""Tests for scripts/ledger.py. Run: python3 -m unittest discover -s tests"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path

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


class UpdateImageTest(unittest.TestCase):
    def test_new_image_records_now_and_each_digest(self):
        result = ledger.update_image(None, ["1.0.0", "1.1.0"], {"1.0.0": digest(1), "1.1.0": digest(2)}.get, NOW)
        self.assertEqual(
            result["releases"],
            [
                {"version": "1.0.0", "releaseTimestamp": NOW, "digest": digest(1)},
                {"version": "1.1.0", "releaseTimestamp": NOW, "digest": digest(2)},
            ],
        )

    def test_known_tags_are_never_looked_up_or_changed(self):
        existing = {"releases": [{"version": "1.0.0", "releaseTimestamp": "2026-01-01T00:00:00Z", "digest": digest(1)}]}
        looked_up = []
        result = ledger.update_image(existing, ["1.0.0", "1.1.0"], lambda t: looked_up.append(t) or digest(9), NOW)
        self.assertEqual(looked_up, ["1.1.0"])
        self.assertEqual(result["releases"][0], existing["releases"][0])
        self.assertEqual(result["releases"][1]["releaseTimestamp"], NOW)

    def test_seed_dates_apply_only_to_a_new_image_and_never_lie_in_the_future(self):
        seed = {"1.0.0": "2026-10-01T00:00:00Z", "1.1.0": "2026-12-01T00:00:00Z"}
        result = ledger.update_image(None, ["1.0.0", "1.1.0", "1"], lambda t: digest(1), NOW, seed)
        by_tag = {r["version"]: r for r in result["releases"]}
        self.assertEqual(by_tag["1.0.0"]["releaseTimestamp"], "2026-10-01T00:00:00Z")
        self.assertEqual(by_tag["1.0.0"]["seed"], "github-release")
        self.assertEqual(by_tag["1.1.0"]["releaseTimestamp"], NOW)
        self.assertNotIn("seed", by_tag["1.1.0"])
        self.assertEqual(by_tag["1"]["releaseTimestamp"], NOW)

        existing = {"releases": [{"version": "1", "releaseTimestamp": NOW, "digest": digest(1)}]}
        later = ledger.update_image(existing, ["1", "1.0.0"], lambda t: digest(2), "2026-11-21T00:00:00Z", seed)
        self.assertEqual(later["releases"][1]["releaseTimestamp"], "2026-11-21T00:00:00Z")

    def test_removed_tags_are_kept_as_deprecated_until_they_return(self):
        existing = {"releases": [{"version": "1.0.0", "releaseTimestamp": NOW, "digest": digest(1)}]}
        gone = ledger.update_image(existing, [], lambda t: digest(2), NOW)
        self.assertTrue(gone["releases"][0]["isDeprecated"])
        back = ledger.update_image(gone, ["1.0.0"], lambda t: digest(2), NOW)
        self.assertNotIn("isDeprecated", back["releases"][0])
        self.assertEqual(back["releases"][0]["digest"], digest(1))

    def test_source_url_is_kept(self):
        result = ledger.update_image(None, [], lambda t: digest(1), NOW, source_url="https://github.com/example/app")
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

    def test_no_new_tags_means_no_commit_and_no_lookups(self):
        existing = {
            "sourceUrl": "https://github.com/example/app",
            "releases": [
                {"version": "1.0.0", "releaseTimestamp": "2026-10-01T00:00:00Z", "digest": digest(1)},
                {"version": "1.1.0", "releaseTimestamp": NOW, "digest": digest(2)},
            ],
        }
        routes, commits = self.routes(existing=existing)
        _, fake, out = self.run_ledger(routes)
        self.assertEqual(commits, [])
        self.assertEqual(fake.heads(), [])
        self.assertIn("No new tags.", out)

    def test_dry_run_writes_nothing(self):
        routes, commits = self.routes()
        _, fake, out = self.run_ledger(routes, apply=False)
        self.assertEqual(commits, [])
        self.assertFalse([c for c in fake.calls if c[0] == "POST"])
        self.assertIn("would write images/ghcr.io/example/app.json (2 releases)", out)


if __name__ == "__main__":
    unittest.main()
