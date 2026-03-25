# Release Downloads Sync

This repo can push published desktop release metadata to the BetterFlow backend so the downloads page stays current automatically.

## Trigger

The workflow [`.github/workflows/sync-downloads.yml`](.github/workflows/sync-downloads.yml) runs when a GitHub release is `published`.

It can also be run manually with `workflow_dispatch` and a `release_tag` input to backfill or repair release metadata.

## GitHub secrets

Add these repository secrets:

- `DOWNLOADS_SYNC_BACKEND_URL`
  Example: `https://api.betterflow.eu`
- `DOWNLOADS_SYNC_TOKEN`
  Shared secret for the backend sync endpoint

## Backend endpoint

Expected endpoint:

- `POST /api/internal/releases/desktop`

Expected headers:

- `Content-Type: application/json`
- `X-Release-Token: <shared secret>`

Expected payload:

```json
{
  "version": "1.4.2",
  "tag_name": "v1.4.2",
  "release_name": "BetterFlow Sync 1.4.2",
  "published_at": "2026-03-23T18:59:10Z",
  "release_url": "https://github.com/Better-Quality-Assurance/betterflow-sync/releases/tag/v1.4.2",
  "changelog_markdown": "## What's changed\n- Faster startup\n- Better tracked time accuracy",
  "macos_arm64_url": "https://github.com/.../BetterFlow-macOS-arm64.dmg",
  "macos_x64_url": "https://github.com/.../BetterFlow-macOS-x86_64.dmg",
  "windows_url": "https://github.com/.../BetterFlow-Windows.zip"
}
```

## Backend behavior

Recommended backend behavior:

- authenticate the request using `X-Release-Token`
- upsert one "latest desktop release" record
- return `200` or `204` on success
- reject incomplete payloads with `4xx`

## Important detail

This sync is intentionally tied to published releases, not tag creation and not draft release creation. That prevents the downloads page from advertising builds that are not yet publicly available.
