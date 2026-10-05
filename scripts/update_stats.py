"""Publish only aggregate statistics from an explicitly configured allowlist."""

import datetime as dt
import html
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class StatsError(Exception):
    pass


def request_json(path, token, params=None):
    url = "https://api.github.com" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "aggregate-profile-stats",
    })
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(min(30, 2 ** (attempt + 1)))
                continue
            raise StatsError("GitHub request failed; check access and rate limits.") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            raise StatsError("GitHub request failed; the previous card was preserved.") from None
    raise StatsError("GitHub request failed.")


def pages(path, token, **params):
    for page in range(1, 1001):
        result = request_json(path, token, dict(params, per_page=100, page=page))
        if not isinstance(result, list):
            raise StatsError("Unexpected GitHub response; no card was published.")
        yield from result
        if len(result) < 100:
            return
    raise StatsError("Pagination limit reached; no partial counts were published.")


def configuration(raw):
    try:
        config = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise StatsError("Configure PRIVATE_STATS_REPOSITORIES as a JSON object.") from None
    if not isinstance(config, dict) or set(config) != {"backend", "frontend"}:
        raise StatsError("Configuration needs backend and frontend repository lists.")
    seen = set()
    for repos in config.values():
        if not isinstance(repos, list) or not repos:
            raise StatsError("Each category needs a nonempty repository list.")
        for repo in repos:
            if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                raise StatsError("Invalid repository configuration.")
            if repo.lower() in seen:
                raise StatsError("A repository must appear in only one category.")
            seen.add(repo.lower())
    return config


def collect(config, token, login, now):
    stats = {"backend": 0, "frontend": 0, "pull_requests": 0, "merged_prs": 0}
    active_days = set()
    months = {}
    for offset in range(11, -1, -1):
        index = now.year * 12 + now.month - 1 - offset
        months[f"{index // 12:04d}-{index % 12 + 1:02d}"] = 0
    cutoff = (now - dt.timedelta(days=365)).date()
    for category, repos in config.items():
        for repo in repos:
            prefix = "/repos/" + repo
            seen = set()
            # All current remote branches, deduplicated by SHA within each repo.
            branches = pages(prefix + "/branches", token)
            for branch in branches:
                for commit in pages(prefix + "/commits", token, author=login, sha=branch["name"]):
                    sha = commit["sha"]
                    if sha in seen or len(commit["parents"]) > 1:
                        continue
                    seen.add(sha)
                    stats[category] += 1
                    date = dt.datetime.fromisoformat(commit["commit"]["author"]["date"].replace("Z", "+00:00")).date()
                    if cutoff <= date <= now.date():
                        active_days.add(date)
                    month = date.strftime("%Y-%m")
                    if month in months:
                        months[month] += 1
            # Search returns aggregate counts without fetching PR titles or bodies.
            query = f"repo:{repo} is:pr author:{login}"
            for key, suffix in (("pull_requests", ""), ("merged_prs", " is:merged")):
                result = request_json("/search/issues", token, {"q": query + suffix, "per_page": 1})
                if result.get("incomplete_results", True) or not isinstance(result.get("total_count"), int):
                    raise StatsError("Incomplete search results; no partial counts were published.")
                stats[key] += result["total_count"]
                time.sleep(2)
    stats["commits"] = stats["backend"] + stats["frontend"]
    stats["active_days"] = len(active_days)
    stats["months"] = months
    return stats


def render(stats, now):
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="880" height="480" viewBox="0 0 880 480" role="img" aria-labelledby="title desc">',
             '<title id="title">Private work in numbers</title>',
             '<desc id="desc">Aggregate commits and pull requests from selected private repositories. No repository identities or private content are included.</desc>',
             '<rect width="880" height="480" rx="20" fill="#0d1117"/>',
             '<rect x=".5" y=".5" width="879" height="479" rx="20" fill="none" stroke="#30363d"/>',
             '<g font-family="Arial, sans-serif">',
             '<text x="32" y="45" fill="#f0f6fc" font-size="25" font-weight="700">PRIVATE WORK / PUBLIC NUMBERS</text>',
             '<text x="32" y="72" fill="#8b949e" font-size="14">Selected private repositories · aggregate statistics only</text>']
    metrics = [("Non-merge commits", stats["commits"]), ("Pull requests opened", stats["pull_requests"]),
               ("Pull requests merged", stats["merged_prs"]), ("Backend commits", stats["backend"]),
               ("Frontend commits", stats["frontend"]), ("Active days / last 365", stats["active_days"])]
    for i, (label, value) in enumerate(metrics):
        x, y = 32 + (i % 3) * 282, 108 + (i // 3) * 94
        parts += [f'<rect x="{x}" y="{y}" width="250" height="78" rx="10" fill="#161b22"/>',
                  f'<text x="{x + 18}" y="{y + 36}" fill="#58a6ff" font-size="30" font-weight="700">{value:,}</text>',
                  f'<text x="{x + 18}" y="{y + 61}" fill="#c9d1d9" font-size="13">{html.escape(label)}</text>']
    parts.append('<text x="32" y="319" fill="#c9d1d9" font-size="14">Monthly commits · latest 12 calendar months</text>')
    maximum = max(max(stats["months"].values()), 1)
    for i, (month, count) in enumerate(stats["months"].items()):
        x = 38 + i * 68
        height = count * 66 / maximum
        label = dt.datetime.strptime(month, "%Y-%m").strftime("%b")
        parts += [f'<rect x="{x}" y="{405-height:.1f}" width="40" height="{height:.1f}" rx="4" fill="#238636"/>',
                  f'<text x="{x+20}" y="{397-height:.1f}" text-anchor="middle" fill="#8b949e" font-size="11">{count}</text>',
                  f'<text x="{x+20}" y="426" text-anchor="middle" fill="#8b949e" font-size="11">{label}</text>']
    parts.append(f'<text x="32" y="458" fill="#8b949e" font-size="12">Refreshed {now:%Y-%m-%d %H:%M UTC} · GitHub Actions · every 6 hours</text></g></svg>')
    return "\n".join(parts) + "\n"


def main():
    token = os.environ.get("PRIVATE_STATS_TOKEN", "")
    if not token:
        raise StatsError("Add PRIVATE_STATS_TOKEN before running the refresh.")
    config = configuration(os.environ.get("PRIVATE_STATS_REPOSITORIES", ""))
    now = dt.datetime.now(dt.timezone.utc)
    stats = collect(config, token, os.environ["STATS_LOGIN"], now)
    output = Path("assets/private-stats.svg")
    output.parent.mkdir(exist_ok=True)
    output.write_text(render(stats, now), encoding="utf-8")
    print("Aggregate stats card refreshed. No private details were published.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never emit API URLs, responses, identities, tokens, or tracebacks.
        print("Stats refresh failed. Check the two repository secrets, token access, and API availability. Previous stats were preserved.", file=sys.stderr)
        sys.exit(1)
