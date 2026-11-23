# image-release-ledger

First-seen release timestamps for container images, consumed by Renovate's
`minimumReleaseAge`.

Renovate reads release dates only from Docker Hub. For ghcr.io, lscr.io,
quay.io, dhi.io and mcr.microsoft.com it gets bare tag names, so it cannot
propose "the newest version that is at least 14 days old". This repository
records when each tag was **first seen** and publishes that as Renovate's
release timestamp. First-seen is never earlier than the real release, so the
soak is at least as strict as one based on the publisher's date.

Status: spike for one image (`ghcr.io/italypaleale/traefik-forward-auth`).
See DevSecNinja/.github ADR 0005 for the cooldown this would replace.

## How it works

1. `.github/workflows/ledger.yml` runs every 6 hours (GitHub may run it less
   often, about twice a day on this repo, see "Schedule"). `scripts/ledger.py`
   lists the tags of every image in [`images.txt`](images.txt) with an
   anonymous registry token, keeps only the tags its `tags=` filter allows,
   reads the manifest digest of each new tag and re-checks the digest of the
   newest exact version tags (`recheck=`, default 20).
2. It writes `images/<registry>/<repository>.json` on the **`data`** branch:

   ```json
   {
     "sourceUrl": "https://github.com/italypaleale/traefik-forward-auth",
     "releases": [
       {
         "version": "4.14.4",
         "releaseTimestamp": "2026-10-21T12:51:55Z",
         "digest": "sha256:99c106f8…",
         "seed": "github-release"
       }
     ]
   }
   ```

3. Renovate reads it as a
   [custom datasource](https://docs.renovatebot.com/modules/datasource/custom/)
   (`overrideDatasource: "custom.first-seen"`, see `renovate.json5`). The
   `digest` becomes the pinned `tag@sha256:…`.

Rules:

- A new tag gets the time it was first seen and its digest at that moment.
- A known tag never changes. Its digest stays the first one seen, so a
  re-pushed (possibly hijacked) tag is not picked up.
- **Moved tags.** The newest exact version tags (`x.y.z`, optional
  pre-release) of each image are re-checked on every run: 20 by default, in
  version order, set per image with `recheck=<n>` in `images.txt` (`0` turns
  the check off). If one now points at another digest, its entry keeps the
  first digest and gets a `retagged` record:

  ```json
  "retagged": [{ "from": "sha256:…", "to": "sha256:…", "seen": "2026-11-20T12:23:00Z" }]
  ```

  The run lists it in the job summary and opens an issue, because a moved
  version tag is a supply-chain signal. The issue URL is stored on the record
  (`issue`); a record without one is retried on the next run, and an open
  issue with the same title is reused.
- **Floating tags** (`latest`, `beta`, `4`, `4.16`) move by design, so a move
  is history, not an alarm. Each keeps a digest history under the top-level
  `floating` key:

  ```json
  "floating": { "latest": [{ "digest": "sha256:…", "seen": "2026-11-13T05:32:35Z" }] }
  ```

  A digest is "seen" at the earliest time it appeared under **any** tag of the
  image, so `latest` pointing at 4.16.0's image inherits 4.16.0's date.
  `releases` lists a floating tag only with its **newest digest that is at
  least `soak-days` old** (`images.txt` option, default 14), dated by that
  digest's first sight. If no digest is old enough, the tag is left out, so
  Renovate never sees an unsoaked digest.
- **Immutable releases.** When `images.txt` names the GitHub source repo, a new
  tag with a matching release records the release's `immutable` flag. GitHub
  immutable releases lock the tag and assets, so `"immutable": true` means
  upstream can't re-point the release (the container tag itself can still
  move, which the retag check covers). The release list is read once per run
  that has new tags.
- Signature and attestation tags (`sha256-<digest>[.sig|.att|.sbom]`) are not
  versions and are left out.
- **Tag filter:** `tags=<regex>` in `images.txt` (anchored with `^` and `$`)
  limits an image to the tags Renovate should consider, for example
  `tags=^v\d+\.\d+\.\d+$`. Other tags are never looked up, stored or shown to
  Renovate. Use it for images that publish many CI tags (`commit-…`, `pr-…`):
  without it every tag costs a manifest request on every run. The filter must
  match the tag the consuming repo pins today. Narrowing a filter drops the
  entries outside it; if a tag matches again later it counts as first seen
  then, so a filter change can only lengthen a soak. The pattern can't contain
  spaces or `#`.
- A tag that disappears from the registry stays, marked `isDeprecated`.
- **Seed:** only when an image is added, its existing tags take the date of
  the matching GitHub release (`seed=github-releases:<owner>/<repo>` in
  `images.txt`), never later than the first run, and are marked
  `"seed": "github-release"`. This is the one place where a date comes from
  upstream instead of from us.

Renovate's custom datasource ignores the extra fields (`seed`, `immutable`,
`retagged`, `floating`); it reads `version`, `releaseTimestamp`, `digest` and
`isDeprecated`.

### Renovate and floating tags

- **Numeric floating tags (`4`, `4.16`) work.** Docker versioning treats them
  as versions, so Renovate reads them from `releases`: the digest update takes
  the ledger's soaked `digest` for that tag (`resolveUpdateDigest`), and ages
  it against that entry's `releaseTimestamp`, which is already past the soak.
  The normal `minimumReleaseAge` passes; no extra setting is needed.
- **Named floating tags (`latest`, `beta`) can't use the ledger yet.** Docker
  versioning doesn't treat them as versions, so Renovate skips the release
  lookup and asks the datasource for the digest directly, and the custom
  datasource's `getDigest()` returns nothing. The ledger still records their
  soaked digests (`releases`, `floating`) for reporting, but **don't point a
  `latest`/`beta` dependency at `custom.first-seen`**: it would never update.
  Keep those on the docker datasource and treat their soak as an open gap
  until Renovate offers a lookup path for them.

## Trust model

- Only the owner and the `ledger.yml` job can write. The job is the only
  workflow with `contents: write` (and `issues: write`, for moved-tag
  issues), and fork PRs get a read-only token.
- The job commits through GraphQL `createCommitOnBranch` with the workflow
  token, so every data commit is signed by GitHub.
- The `data` branch cannot be deleted or force-pushed (ruleset `data-branch`).
- Failure is safe: if the job stops or an image or tag is missing, Renovate
  sees no new version, so updates stall instead of merging unsoaked versions.
- **Bounded move alarm.** Only the newest `recheck` version tags per image
  are re-checked for a moved digest. An older tag that moves still keeps its
  first digest in the ledger, so Renovate never pins the moved image; the only
  loss is the alarm issue for that tag. Renovate proposes recent versions, so
  the newest 20 cover what it can pick. Raise `recheck=` for an image whose
  older tags matter.

## Limits and schedule

- **Requests per run.** Each image costs its tag-list pages, a manifest
  request per new tag, one per re-checked version tag (newest 20) and one per
  floating tag. For the 16 truenas-apps images that is about 360 manifest
  requests a run instead of about 1,660 when every tag was re-checked.
- **Pacing.** Registry requests are sequential, with a 0.1 s pause between
  them.
- **Registry limits.** A tag list is read in pages of 1,000, up to 500 pages
  per image. A 429 or 5xx response, including one to a token request, is
  retried up to twice: after `Retry-After` (seconds or an HTTP date), or else
  after 5 s and then 10 s plus up to 1 s of random jitter, each wait capped
  at 60 s. ghcr.io names its limit, 44,000 requests a minute, only in the
  response body. If the registry still refuses, the image fails for the run,
  and the run skips the remaining images on that registry rather than add
  load (fails closed). An anonymous token that expires mid-run is renewed
  once.
- **Schedule.** GitHub delays scheduled workflows on quiet repositories: the
  6-hourly cron has run about every 12 hours here. That only delays new tags
  becoming visible; it never shortens a soak.

## Adding an image

Add a line to `images.txt` (with a `seed=` source if the project publishes
GitHub releases, and a `tags=` filter if it publishes many tags Renovate
doesn't need), then point the image's Renovate rule at `custom.first-seen`.
Docker Hub images don't belong here: Renovate already has their release dates.

## Development

Python 3.11+ (the runner's `python3`), stdlib only.

```sh
python3 -m unittest discover -s tests
GITHUB_TOKEN=… GITHUB_REPOSITORY=DevSecNinja/image-release-ledger python3 scripts/ledger.py   # dry run
```
