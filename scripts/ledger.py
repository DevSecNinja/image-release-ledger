"""Record when each container image tag was first seen, for Renovate.

Registries such as ghcr.io, lscr.io, quay.io, dhi.io and mcr.microsoft.com
expose no release dates, so Renovate's minimumReleaseAge cannot soak their
images. This script lists every tag of the images in images.txt, records the
time it first saw each tag together with the tag's manifest digest, and writes
one JSON file per image in the format of Renovate's custom datasource:

    images/<registry>/<repository>.json -> {"releases": [{"version", "releaseTimestamp", "digest"}]}

The files live on the `data` branch. Rules:

- A new tag gets the current time and its current digest.
- A known tag is never changed: its timestamp never moves and its digest stays
  the first one seen, so a re-pushed (possibly hijacked) tag is not picked up.
- A tag that disappears from the registry is kept and marked isDeprecated.
- Only when an image is added (its file does not exist yet) and images.txt
  names a `seed=github-releases:<owner>/<repo>` source do its existing tags
  take the date of the matching GitHub release instead (never later than now),
  marked "seed": "github-release". Every tag seen after that is first-seen.

Commits go to the data branch through GraphQL createCommitOnBranch with the
workflow's GITHUB_TOKEN, so GitHub signs them. Stdlib only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

API = "https://api.github.com"
USER_AGENT = "image-release-ledger"
MANIFEST_ACCEPT = "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
# An explicit registry host (with a dot) and a path without empty or relative
# segments. Docker Hub images have native release dates and are refused.
IMAGE = re.compile(
    r"^(?P<host>[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?)/(?P<repo>[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*)$"
)
SEED = re.compile(r"^github-releases:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$")
LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')

Response = tuple[int, dict[str, str], bytes]
Transport = Callable[[str, str, dict[str, str], bytes | None], Response]


class LedgerError(Exception):
    pass


def urllib_transport(method: str, url: str, headers: dict[str, str], data: bytes | None) -> Response:
    if not url.startswith("https://"):
        raise LedgerError(f"refusing non-HTTPS URL {url}")
    request = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_images(text: str) -> list[dict]:
    """images.txt: one image per line, optional `key=value` options, `#` comments."""
    images = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, *options = line.split()
        match = IMAGE.match(name)
        if not match or match["host"] == "docker.io" or ".." in name:
            raise LedgerError(f"images.txt line {number}: {name!r} is not a non-Docker-Hub image with a registry host")
        image = {"name": name, "host": match["host"], "repo": match["repo"], "seed": None}
        for option in options:
            key, _, value = option.partition("=")
            seed = SEED.match(value) if key == "seed" else None
            if not seed:
                raise LedgerError(f"images.txt line {number}: unknown option {option!r}")
            image["seed"] = seed["repo"]
        images.append(image)
    return images


class Registry:
    """OCI distribution API with anonymous bearer tokens."""

    def __init__(self, transport: Transport, host: str):
        self.transport = transport
        self.host = host
        self.tokens: dict[str, str] = {}

    def _token(self, repo: str, challenge: str) -> str:
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.pop("realm", "")
        if not challenge.lower().startswith("bearer") or not realm.startswith("https://"):
            raise LedgerError(f"{self.host}/{repo}: unsupported auth challenge {challenge!r}")
        params.setdefault("scope", f"repository:{repo}:pull")
        status, _, body = self.transport("GET", f"{realm}?{urllib.parse.urlencode(params)}", {}, None)
        if status != 200:
            raise LedgerError(f"{self.host}/{repo}: token request failed with HTTP {status}")
        payload = json.loads(body)
        token = payload.get("token") or payload.get("access_token")
        if not token:
            raise LedgerError(f"{self.host}/{repo}: token response has no token")
        return token

    def _request(self, method: str, url: str, repo: str, headers: dict[str, str]) -> Response:
        auth = {"Authorization": f"Bearer {self.tokens[repo]}"} if repo in self.tokens else {}
        status, response_headers, body = self.transport(method, url, {**headers, **auth}, None)
        if status == 401 and repo not in self.tokens:
            self.tokens[repo] = self._token(repo, response_headers.get("www-authenticate", ""))
            return self._request(method, url, repo, headers)
        return status, response_headers, body

    def tags(self, repo: str) -> list[str]:
        url = f"https://{self.host}/v2/{repo}/tags/list?n=1000"
        tags: list[str] = []
        for _ in range(100):
            status, headers, body = self._request("GET", url, repo, {"Accept": "application/json"})
            if status != 200:
                raise LedgerError(f"{self.host}/{repo}: tag list failed with HTTP {status}")
            tags += json.loads(body).get("tags") or []
            link = LINK_NEXT.search(headers.get("link", ""))
            if not link:
                return tags
            url = urllib.parse.urljoin(url, link.group(1))
        raise LedgerError(f"{self.host}/{repo}: tag list has more than 100 pages")

    def digest(self, repo: str, tag: str) -> str:
        url = f"https://{self.host}/v2/{repo}/manifests/{urllib.parse.quote(tag, safe='')}"
        status, headers, _ = self._request("HEAD", url, repo, {"Accept": MANIFEST_ACCEPT})
        digest = headers.get("docker-content-digest", "")
        if status != 200 or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise LedgerError(f"{self.host}/{repo}:{tag}: no manifest digest (HTTP {status})")
        return digest


def update_image(
    existing: dict | None,
    tags: list[str],
    digest_of: Callable[[str], str],
    now: str,
    seed_dates: dict[str, str] | None = None,
    source_url: str | None = None,
) -> dict:
    """The image's new ledger. Only tags missing from `existing` are looked up."""
    releases = {r["version"]: dict(r) for r in (existing or {}).get("releases", [])}
    if existing is not None:
        seed_dates = None
    current = set(tags)
    for tag in tags:
        if tag in releases:
            releases[tag].pop("isDeprecated", None)
            continue
        entry = {"version": tag, "releaseTimestamp": now, "digest": digest_of(tag)}
        seeded = (seed_dates or {}).get(tag)
        if seeded and seeded < now:
            entry["releaseTimestamp"] = seeded
            entry["seed"] = "github-release"
        releases[tag] = entry
    for tag, entry in releases.items():
        if tag not in current:
            entry["isDeprecated"] = True
    ledger = {"releases": sorted(releases.values(), key=lambda r: (r["releaseTimestamp"], r["version"]))}
    source_url = source_url or (existing or {}).get("sourceUrl")
    return {"sourceUrl": source_url, **ledger} if source_url else ledger


def dump(ledger: dict) -> str:
    return json.dumps(ledger, indent=2) + "\n"


class GitHub:
    def __init__(self, transport: Transport, token: str, repository: str):
        self.transport = transport
        self.repository = repository
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    def rest(self, method: str, path: str, body: dict | None = None, ok=(200,)) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        status, _, raw = self.transport(method, f"{API}/{path}", self.headers, data)
        if status not in ok:
            raise LedgerError(f"{method} {path}: HTTP {status}")
        return status, (json.loads(raw) if raw else {})

    def graphql(self, query: str, variables: dict) -> dict:
        _, payload = self.rest("POST", "graphql", {"query": query, "variables": variables})
        if payload.get("errors"):
            raise LedgerError(f"GraphQL: {payload['errors']}")
        return payload["data"]

    def branch_head(self, branch: str) -> str:
        """The branch's head SHA; creates the branch from the default branch if it is missing."""
        status, ref = self.rest("GET", f"repos/{self.repository}/git/ref/heads/{branch}", ok=(200, 404))
        if status == 200:
            return ref["object"]["sha"]
        _, repo = self.rest("GET", f"repos/{self.repository}")
        _, base = self.rest("GET", f"repos/{self.repository}/git/ref/heads/{repo['default_branch']}")
        sha = base["object"]["sha"]
        self.rest("POST", f"repos/{self.repository}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha}, ok=(201,))
        return sha

    def read_file(self, branch: str, path: str) -> dict | None:
        quoted = urllib.parse.quote(path)
        status, payload = self.rest("GET", f"repos/{self.repository}/contents/{quoted}?ref={branch}", ok=(200, 404))
        if status == 404:
            return None
        return json.loads(base64.b64decode(payload["content"]))

    def release_dates(self, repo: str) -> dict[str, str]:
        """Tag name (with and without a leading v) -> published_at of each published release."""
        dates: dict[str, str] = {}
        for page in range(1, 11):
            _, releases = self.rest("GET", f"repos/{repo}/releases?per_page=100&page={page}")
            for release in releases:
                if release.get("draft") or not release.get("published_at"):
                    continue
                tag = release["tag_name"]
                for name in (tag, tag.removeprefix("v")):
                    dates.setdefault(name, release["published_at"])
            if len(releases) < 100:
                break
        return dates

    def commit(self, branch: str, head: str, files: dict[str, str], headline: str, body: str) -> str:
        additions = [{"path": p, "contents": base64.b64encode(c.encode()).decode()} for p, c in sorted(files.items())]
        data = self.graphql(
            "mutation($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) { commit { oid } } }",
            {
                "input": {
                    "branch": {"repositoryNameWithOwner": self.repository, "branchName": branch},
                    "expectedHeadOid": head,
                    "message": {"headline": headline, "body": body},
                    "fileChanges": {"additions": additions},
                }
            },
        )
        return data["createCommitOnBranch"]["commit"]["oid"]


def ledger_path(image: dict) -> str:
    return f"images/{image['name']}.json"


def run(images: list[dict], github: GitHub, transport: Transport, branch: str, now: str, apply: bool) -> int:
    head = github.branch_head(branch) if apply else ""
    changed: dict[str, str] = {}
    added: list[str] = []
    failures = 0
    for image in images:
        path = ledger_path(image)
        try:
            existing = github.read_file(branch, path)
            registry = Registry(transport, image["host"])
            tags = registry.tags(image["repo"])
            seed = github.release_dates(image["seed"]) if image["seed"] and existing is None else None
            source = f"https://github.com/{image['seed']}" if image["seed"] else None

            def digest_of(tag: str, registry: Registry = registry, repo: str = image["repo"]) -> str:
                return registry.digest(repo, tag)

            ledger = update_image(existing, tags, digest_of, now, seed, source)
        except (LedgerError, ValueError, KeyError, OSError) as error:
            print(f"::error::{image['name']}: {error}")
            failures += 1
            continue
        if existing is None or dump(ledger) != dump(existing):
            changed[path] = dump(ledger)
            known = {r["version"] for r in (existing or {}).get("releases", [])}
            added += [f"{image['name']}:{r['version']}" for r in ledger["releases"] if r["version"] not in known]
    if not changed:
        print("No new tags.")
    elif apply:
        body = "\n".join(f"- {tag}" for tag in added[:200]) or "Deprecation flags only."
        oid = github.commit(branch, head, changed, f"chore(data): record {len(added)} first-seen tag(s)", body)
        print(f"Committed {len(changed)} file(s) to {branch}: {oid}")
    else:
        for path, content in changed.items():
            print(f"would write {path} ({len(json.loads(content)['releases'])} releases)")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record first-seen times of container image tags.")
    parser.add_argument("--images", default="images.txt")
    parser.add_argument("--branch", default="data")
    parser.add_argument("--apply", action="store_true", help="commit to the data branch (default: dry run)")
    args = parser.parse_args(argv)
    token, repository = os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repository:
        print("::error::GITHUB_TOKEN and GITHUB_REPOSITORY are required", file=sys.stderr)
        return 2
    try:
        images = parse_images(Path(args.images).read_text(encoding="utf-8"))
        github = GitHub(urllib_transport, token, repository)
        return run(images, github, urllib_transport, args.branch, now_iso(), args.apply)
    except LedgerError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
