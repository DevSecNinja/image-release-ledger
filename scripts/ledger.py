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
- Version tags (x.y.z, optional pre-release) are re-checked on every run. If
  one now points at another digest, its entry keeps the first digest and gets
  a `retagged` record ({from, to, seen}); the run reports it in the job summary
  and opens an issue.
- Floating tags (latest, beta, 4, 4.16) move by design. Each keeps a digest
  history under the top-level `floating` key, where a digest is "seen" at the
  earliest time it appeared under any tag of the image. `releases` exposes a
  floating tag only with its newest digest that is at least `soak-days` old
  (images.txt option, default 14), dated by that digest's first sight; with no
  such digest the tag is left out. Renovate therefore only ever sees soaked
  digests for these tags.
- Signature and attestation tags (sha256-<digest>[.sig|.att|.sbom]) are not
  versions and are left out.
- When images.txt names the GitHub source repo, a new tag that has a release
  records the release's `immutable` flag (GitHub immutable releases lock the
  tag and assets). The release list is read once per run that has new tags.
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
import copy
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

API = "https://api.github.com"
USER_AGENT = "image-release-ledger"
MANIFEST_ACCEPT = "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"
# An explicit registry host (with a dot) and a path without empty or relative
# segments. Docker Hub images have native release dates and are refused.
IMAGE = re.compile(
    r"^(?P<host>[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?)/(?P<repo>[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*)$"
)
# Exact versions: re-checked for moved tags. Rolling tags (latest, 4, 4.16) are not.
VERSION_TAG = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
# Cosign signatures, attestations and SBOMs stored as tags: not versions.
ARTIFACT_TAG = re.compile(r"^sha256-[0-9a-f]{64}(?:\.(?:sig|att|sbom))?$")
SOAK = re.compile(r"^\d{1,3}$")
DEFAULT_SOAK_DAYS = 14
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
        image["soak_days"] = DEFAULT_SOAK_DAYS
        for option in options:
            key, _, value = option.partition("=")
            if key == "seed" and SEED.match(value):
                image["seed"] = SEED.match(value)["repo"]
            elif key == "soak-days" and SOAK.match(value):
                image["soak_days"] = int(value)
            else:
                raise LedgerError(f"images.txt line {number}: unknown option {option!r}")
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


def shift(timestamp: str, days: int) -> str:
    """`timestamp` (YYYY-MM-DDTHH:MM:SSZ) moved back by `days`."""
    moment = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (moment - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def update_image(
    existing: dict | None,
    tags: list[str],
    digest_of: Callable[[str], str],
    now: str,
    releases_info: dict[str, dict] | None = None,
    source_url: str | None = None,
    soak_days: int = DEFAULT_SOAK_DAYS,
) -> tuple[dict, list[dict]]:
    """The image's new ledger and its new retag events.

    New tags, exact version tags and floating tags are looked up; nothing else.
    `releases_info` maps a tag to its GitHub release ({"published_at",
    "immutable"}); its date seeds tags only for a new image (`existing` None).
    """
    tags = [t for t in tags if not ARTIFACT_TAG.match(t)]
    releases = {}
    floating = copy.deepcopy((existing or {}).get("floating", {}))
    for r in copy.deepcopy((existing or {}).get("releases", [])):
        if ARTIFACT_TAG.match(r["version"]):
            continue
        if VERSION_TAG.match(r["version"]):
            releases[r["version"]] = r
        elif r["version"] not in floating:
            # An entry written before floating tags had a history.
            floating[r["version"]] = [{"digest": r["digest"], "seen": r["releaseTimestamp"]}]
    seeding = existing is None
    current = set(tags)
    events: list[dict] = []
    for tag in (t for t in tags if VERSION_TAG.match(t)):
        entry = releases.get(tag)
        if entry is not None:
            entry.pop("isDeprecated", None)
            digest = digest_of(tag)
            history = entry.get("retagged", [])
            last = history[-1]["to"] if history else entry["digest"]
            if digest != last:
                record = {"from": last, "to": digest, "seen": now}
                entry["retagged"] = [*history, record]
                events.append({"tag": tag, **record})
            continue
        entry = {"version": tag, "releaseTimestamp": now, "digest": digest_of(tag)}
        release = (releases_info or {}).get(tag)
        if release:
            if seeding and release.get("published_at") and release["published_at"] < now:
                entry["releaseTimestamp"] = release["published_at"]
                entry["seed"] = "github-release"
            if isinstance(release.get("immutable"), bool):
                entry["immutable"] = release["immutable"]
        releases[tag] = entry
    for tag, entry in releases.items():
        if tag not in current:
            entry["isDeprecated"] = True

    # When each digest was first seen under any tag of the image.
    first_seen: dict[str, str] = {}
    sightings = [(e["digest"], e["releaseTimestamp"]) for e in releases.values()]
    sightings += [(r["to"], r["seen"]) for e in releases.values() for r in e.get("retagged", [])]
    sightings += [(h["digest"], h["seen"]) for history in floating.values() for h in history]
    for digest, seen in sightings:
        first_seen[digest] = min(seen, first_seen.get(digest, seen))
    for history in floating.values():
        for h in history:
            h["seen"] = first_seen[h["digest"]]
    cutoff = shift(now, soak_days)
    exposed = []
    for tag in sorted(floating.keys() | {t for t in current if not VERSION_TAG.match(t)}):
        history = floating.setdefault(tag, [])
        if tag in current:
            digest = digest_of(tag)
            if not history or history[-1]["digest"] != digest:
                history.append({"digest": digest, "seen": min(now, first_seen.get(digest, now))})
                first_seen.setdefault(digest, history[-1]["seen"])
        soaked = next((h for h in reversed(history) if h["seen"] <= cutoff), None)
        if soaked:
            entry = {"version": tag, "releaseTimestamp": soaked["seen"], "digest": soaked["digest"]}
            if tag not in current:
                # Gone from the registry: keep it visible, like an exact tag.
                entry["isDeprecated"] = True
            exposed.append(entry)

    entries = sorted([*releases.values(), *exposed], key=lambda r: (r["releaseTimestamp"], r["version"]))
    ledger = {"releases": entries}
    if floating:
        ledger["floating"] = dict(sorted(floating.items()))
    source_url = source_url or (existing or {}).get("sourceUrl")
    return ({"sourceUrl": source_url, **ledger} if source_url else ledger), events


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

    def releases(self, repo: str) -> dict[str, dict]:
        """Tag name (with and without a leading v) -> {published_at, immutable} of each published release."""
        found: dict[str, dict] = {}
        for page in range(1, 11):
            _, releases = self.rest("GET", f"repos/{repo}/releases?per_page=100&page={page}")
            for release in releases:
                if release.get("draft") or not release.get("published_at"):
                    continue
                tag = release["tag_name"]
                info = {"published_at": release["published_at"], "immutable": release.get("immutable")}
                for name in (tag, tag.removeprefix("v")):
                    found.setdefault(name, info)
            if len(releases) < 100:
                break
        return found

    def open_issues_by_title(self) -> dict[str, str]:
        """Title -> URL of every open issue (not pull request), all pages."""
        titles: dict[str, str] = {}
        for page in range(1, 51):
            _, issues = self.rest("GET", f"repos/{self.repository}/issues?state=open&per_page=100&page={page}")
            for issue in issues:
                if not issue.get("pull_request"):
                    titles.setdefault(issue["title"], issue.get("html_url", ""))
            if len(issues) < 100:
                return titles
        raise LedgerError("more than 5000 open issues")

    def comment_issue(self, url: str, body: str) -> None:
        match = re.search(r"/issues/(\d+)$", url)
        if not match:
            raise LedgerError(f"not an issue URL: {url}")
        self.rest("POST", f"repos/{self.repository}/issues/{match[1]}/comments", {"body": body}, ok=(201,))

    def open_issue(self, title: str, body: str) -> str:
        _, issue = self.rest("POST", f"repos/{self.repository}/issues", {"title": title, "body": body}, ok=(201,))
        return issue.get("html_url", "")

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
    results: list[tuple[dict, str, dict | None, dict, set[str]]] = []
    events: list[dict] = []
    failures = 0
    for image in images:
        path = ledger_path(image)
        try:
            existing = github.read_file(branch, path)
            registry = Registry(transport, image["host"])
            tags = registry.tags(image["repo"])
            known = {r["version"] for r in (existing or {}).get("releases", [])}
            known |= set((existing or {}).get("floating", {}))
            new_tags = [t for t in tags if t not in known and not ARTIFACT_TAG.match(t)]
            info = github.releases(image["seed"]) if image["seed"] and new_tags else None
            source = f"https://github.com/{image['seed']}" if image["seed"] else None

            def digest_of(tag: str, registry: Registry = registry, repo: str = image["repo"]) -> str:
                return registry.digest(repo, tag)

            ledger, image_events = update_image(existing, tags, digest_of, now, info, source, image["soak_days"])
        except (LedgerError, ValueError, KeyError, OSError) as error:
            print(f"::error::{image['name']}: {error}")
            failures += 1
            continue
        events += [{"image": image["name"], **e} for e in image_events]
        results.append((image, path, existing, ledger, known))
    for event in events:
        print(f"::warning::{event['image']}:{event['tag']} moved from {event['from']} to {event['to']}")
    write_summary(events)
    # Before the commit, so the issue URLs are stored with the records; a record
    # without one is retried on the next run.
    failures += notify_retags(github, [(image, ledger) for image, _, _, ledger, _ in results], apply)
    changed: dict[str, str] = {}
    added: list[str] = []
    for image, path, existing, ledger, known in results:
        if existing is None or dump(ledger) != dump(existing):
            changed[path] = dump(ledger)
            added += [f"{image['name']}:{r['version']}" for r in ledger["releases"] if r["version"] not in known]
    if not changed:
        print("No new tags.")
    elif apply:
        lines = [f"- {tag}" for tag in added[:200]] + [f"- retagged {e['image']}:{e['tag']}" for e in events]
        body = "\n".join(lines) or "Deprecation flags and notifications only."
        oid = github.commit(branch, head, changed, f"chore(data): record {len(added)} first-seen tag(s)", body)
        print(f"Committed {len(changed)} file(s) to {branch}: {oid}")
    else:
        for path, content in changed.items():
            print(f"would write {path} ({len(json.loads(content)['releases'])} releases)")
    return 1 if failures else 0


def write_summary(events: list[dict]) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not events or not summary:
        return
    with open(summary, "a", encoding="utf-8") as handle:
        handle.write("## Moved tags\n\n")
        handle.writelines(
            f"- `{e['image']}:{e['tag']}`: `{e['from']}` → `{e['to']}` (seen {e['seen']})\n" for e in events
        )


def notify_retags(github: GitHub, ledgers: list[tuple[dict, dict]], apply: bool) -> int:
    """Open one issue per retag record that has none yet; returns the number of failures.

    The issue URL is stored on the record (`issue`), so a failed request is
    retried on the next run, and an open issue with the same title is reused
    instead of opening a duplicate.
    """
    pending = [
        (image, entry, record)
        for image, ledger in ledgers
        for entry in ledger["releases"]
        for record in entry.get("retagged", [])
        if not record.get("issue")
    ]
    if not pending:
        return 0
    if not apply:
        for image, entry, record in pending:
            print(f"would open an issue for {image['name']}:{entry['version']} ({record['to']})")
        return 0
    try:
        by_title = github.open_issues_by_title()
    except (LedgerError, OSError) as error:
        print(f"::error::could not list issues, moved-tag notifications retry next run: {error}")
        return 1
    failed = 0
    for image, entry, record in pending:
        title = "Tag moved: " + image["name"] + ":" + entry["version"]
        body = (
            f"The tag `{image['name']}:{entry['version']}` now points at another image.\n\n"
            f"- First-seen digest (Renovate keeps using it): `{entry['digest']}`\n"
            f"- Previous digest: `{record['from']}`\n"
            f"- New digest: `{record['to']}`\n- Seen: {record['seen']}\n\n"
            "A moved version tag can be a re-release or a supply-chain compromise. "
            "Check the upstream project before trusting the new digest."
        )
        try:
            if title in by_title:
                # Still open from an earlier move: add this transition to it.
                github.comment_issue(by_title[title], body)
                record["issue"] = by_title[title]
            else:
                record["issue"] = by_title[title] = github.open_issue(title, body)
            print(f"Moved-tag issue: {record['issue']}")
        except (LedgerError, OSError) as error:
            print(f"::error::could not open an issue for {image['name']}:{entry['version']}: {error}")
            failed += 1
    return failed


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
