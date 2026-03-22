# Automatic Downloads Badge Updates

The cumulative downloads badge in the README is automatically updated daily via GitHub Actions.

## How It Works

A scheduled GitHub Actions workflow (`.github/workflows/update-downloads-badge.yml`) runs every day at **00:00 UTC** and:

1. **Fetches release data** from the GitHub API
   - Queries all releases in the repository
   - Sums all asset download counts across all versions

2. **Formats the count**
   - Displays as "38K+" for counts ≥ 1,000
   - Displays as "2M+" for counts ≥ 1,000,000
   - Displays raw number for counts < 1,000

3. **Updates the README**
   - Replaces the badge URL with the new count
   - Uses Python regex for reliable string replacement

4. **Commits the change**
   - Creates an automated commit with the updated count
   - Includes Copilot co-author trailer

## Manual Trigger

You can also manually trigger the workflow from the **GitHub Actions** tab by:
1. Going to the repo's Actions page
2. Selecting "Update Downloads Badge" workflow
3. Clicking "Run workflow"

## Badge URL Format

The badge is displayed using shields.io:
```markdown
![Downloads](https://img.shields.io/badge/downloads-38K+-brightgreen.svg)
```

The workflow automatically updates the count portion (`38K+`) while keeping the badge format consistent.

## Notes

- The workflow runs on `ubuntu-latest` with no external dependencies (uses built-in `curl`, `jq`, and `python3`)
- The badge will update automatically without any manual intervention
- If there are no changes to commit (count stays the same), the workflow skips the commit
