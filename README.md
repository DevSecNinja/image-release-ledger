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

1. `.github/workflows/ledger.yml` runs every 6 hours. `scripts/ledger.py`
   lists the tags of every image in [`images.txt`](images.txt) with an
   anonymous registry token and reads the manifest digest of each new tag.
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
- A tag that disappears from the registry stays, marked `isDeprecated`.
- **Seed:** only when an image is added, its existing tags take the date of
  the matching GitHub release (`seed=github-releases:<owner>/<repo>` in
  `images.txt`), never later than the first run, and are marked
  `"seed": "github-release"`. This is the one place where a date comes from
  upstream instead of from us.

## Trust model

- Only the owner and the `ledger.yml` job can write. The job is the only
  workflow with `contents: write`, and fork PRs get a read-only token.
- The job commits through GraphQL `createCommitOnBranch` with the workflow
  token, so every data commit is signed by GitHub.
- The `data` branch cannot be deleted or force-pushed (ruleset `data-branch`).
- Failure is safe: if the job stops or an image or tag is missing, Renovate
  sees no new version, so updates stall instead of merging unsoaked versions.

## Adding an image

Add a line to `images.txt` (with a `seed=` source if the project publishes
GitHub releases), then point the image's Renovate rule at
`custom.first-seen`. Docker Hub images don't belong here: Renovate already has
their release dates.

## Development

Python 3.11+ (the runner's `python3`), stdlib only.

```sh
python3 -m unittest discover -s tests
GITHUB_TOKEN=… GITHUB_REPOSITORY=DevSecNinja/image-release-ledger python3 scripts/ledger.py   # dry run
```
