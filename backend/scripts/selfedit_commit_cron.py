"""Commit the agent's own self-edits to git, nightly.

The dreams cron rewrites SOUL.md, the SKILL.md files, skill code and the
semantic-pattern store on its own every night, and nothing ever committed any of
it. By 2026-08-17 that had silently accumulated to 18 modified files going back
a week; within a day of clearing it a fresh batch (a whole new run_nb_bb plus an
untracked build_nb.py) had already landed. Every session that opened the repo
had to reason about a permanently dirty tree, and because the edits lived only
on disk an S1 remediation pass would have taken them with it -- see the
file-deletion incident playbook.

So: commit them. This does not make the edits any more live (they already are --
the gateway loads from disk), it makes them recoverable and reviewable.

PUSH POLICY (changed 2026-11-12). Until then this deliberately did NOT push
(Brian, 2026-08-17), so local main silently drifted from origin/main (2 ahead /
3 behind by 11/12) and every PR merge left the working tree stale. Brian,
2026-11-12: "The GH is your to manage ... find a best practice". So now, after
committing, publish(): fetch -> rebase onto origin/main -> push
agent/self-edits-YYYYMMDD -> PR -> squash-merge once mergeable -> delete the
branch -> reset local main to origin/main (only when trees are identical).
Any failure aborts cleanly and DMs Brian; nothing is force-pushed to main.

Run as a cron in cron_supervisor.py (daily, after dreams and the 03:00 backup).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent.parent  # deer-flow/
COMMIT_HOUR_LOCAL = 3
COMMIT_MINUTE_LOCAL = 30
# Never snapshot a file mid-write: dreams applies its edits over several
# minutes, so require the tree to have been still for a while first.
QUIESCE_SECONDS = 15 * 60
MAX_FILES_IN_MESSAGE = 40
GIT_TIMEOUT = 120

NEWLINE = chr(10)
_TRAILER = "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"


def _slack_alert(text: str) -> None:
    """Best-effort DM to the owner. Never raises."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    owner = os.environ.get("SLACK_OWNER_USER_ID")
    if not token or not owner:
        return
    try:
        from slack_sdk import WebClient
        client = WebClient(token=token)
        ch = client.conversations_open(users=[owner])["channel"]["id"]
        client.chat_postMessage(channel=ch, text=text)
    except Exception as e:
        logger.warning("[selfedit-commit] slack alert failed: %s", e)


def _git_in(repo: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT, check=True,
    )


def _git(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return _git_in(REPO, *args, stdin=stdin)


def _dirty_paths() -> list[str]:
    """Every dirty or untracked path, from porcelain -z (NUL-safe for spaces)."""
    out = _git("status", "--porcelain", "-z").stdout
    paths = []
    for chunk in out.split("\0"):
        if len(chunk) > 3:
            paths.append(chunk[3:])
    return paths


def _mid_operation(repo: Path = REPO) -> str | None:
    """Name any in-progress git operation we must not commit over."""
    gitdir = repo / ".git"
    if gitdir.is_file():  # submodule: .git is a file pointing at the real dir
        try:
            target = gitdir.read_text(encoding="utf-8").split("gitdir:", 1)[1].strip()
            gitdir = Path(target)
            if not gitdir.is_absolute():
                gitdir = (repo / gitdir).resolve()
        except (OSError, IndexError):
            return None
    for marker, label in (
        ("MERGE_HEAD", "merge"),
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("BISECT_LOG", "bisect"),
    ):
        if (gitdir / marker).exists():
            return label
    return None


def _seconds_since_newest_change(paths: list[str]) -> float:
    """Age of the most recently touched dirty file. Large if unmeasurable."""
    newest = 0.0
    for rel in paths:
        try:
            newest = max(newest, (REPO / rel).stat().st_mtime)
        except OSError:
            continue  # deleted, or a directory entry -- can't gate on it
    if newest == 0.0:
        return float(QUIESCE_SECONDS + 1)
    return time.time() - newest


def _broken_python(paths: list[str]) -> list[str]:
    """Changed .py files that no longer compile."""
    # ast.parse, not py_compile: py_compile insists on writing a .pyc, and
    # cfile=os.devnull raises FileExistsError on Windows ("nul" already exists).
    # A syntax check is all we want here anyway.
    import ast
    broken = []
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        p = REPO / rel
        if not p.exists():
            continue
        try:
            ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=rel)
        except SyntaxError as e:
            broken.append(f"{rel}: line {e.lineno}: {e.msg}")
        except OSError as e:
            broken.append(f"{rel}: unreadable ({e})")
    return broken


def _build_message(staged: list[str], broken: list[str]) -> str:
    shown = staged[:MAX_FILES_IN_MESSAGE]
    more = len(staged) - len(shown)
    lines = [
        "chore(self-edit): nightly agent self-edits",
        "",
        "Applied by the agent's own background jobs (dreams et al) and committed",
        "by the selfedit-commit cron so they are recoverable and reviewable",
        "rather than living only on disk. Not pushed.",
        "",
        f"{len(staged)} file(s):",
    ]
    lines += [f"  {f}" for f in shown]
    if more:
        lines.append(f"  ... and {more} more")
    if broken:
        # Committing broken syntax on purpose: recoverable in history beats lost
        # on disk. The Slack alert is what asks for a human.
        lines += ["", "WARNING - changed Python that does not compile:"]
        lines += [f"  {b}" for b in broken]
    lines += ["", _TRAILER]
    return "\n".join(lines) + "\n"


def commit_once() -> str | None:
    """Commit the current self-edits. Returns the short sha, or None if skipped."""
    op = _mid_operation()
    if op:
        logger.warning("[selfedit-commit] %s in progress -- skipping this run", op)
        return None

    paths = _dirty_paths()
    if not paths:
        logger.info("[selfedit-commit] tree clean; nothing to commit")
        return None

    idle = _seconds_since_newest_change(paths)
    if idle < QUIESCE_SECONDS:
        logger.info("[selfedit-commit] tree touched %.0fs ago (<%ds) -- letting it settle",
                    idle, QUIESCE_SECONDS)
        return None

    broken = _broken_python(paths)

    _git("add", "-A")
    staged = [p for p in _git("diff", "--cached", "--name-only").stdout.splitlines() if p]
    if not staged:
        logger.info("[selfedit-commit] nothing staged after add; skipping")
        return None

    _git("commit", "-F", "-", stdin=_build_message(staged, broken))
    sha = _git("rev-parse", "--short", "HEAD").stdout.strip()
    logger.info("[selfedit-commit] committed %d file(s) as %s", len(staged), sha)

    if broken:
        _slack_alert(
            f":warning: *self-edit commit {sha} includes Python that does not compile.*\n"
            "Committed anyway so it is recoverable, but a background job has likely "
            "broken itself and needs a look:\n```" + "\n".join(broken[:10]) + "```"
        )
    return sha


def bump_parent_pointer() -> str | None:
    """Commit the submodule pointer in the parent repo, if it moved.

    Without this the cleanup just moves up a level: deer-flow goes clean and the
    parent sits permanently at " M deer-flow" instead. Stages ONLY the submodule
    path -- the parent holds hand-maintained files (deploy/, scripts/) and
    sweeping those into an automated commit is not this job.
    """
    parent = REPO.parent
    if not (parent / ".git").exists():
        return None
    op = _mid_operation(parent)
    if op:
        logger.warning("[selfedit-commit] parent %s in progress -- not bumping pointer", op)
        return None
    sub = REPO.name
    status = _git_in(parent, "status", "--porcelain", "--", sub).stdout.strip()
    # Cheap pre-filter only. In porcelain v1 a submodule reads " M" both when the
    # recorded commit moved AND when it merely has dirty content -- the two are not
    # distinguishable here, so this can pass when nothing actually moved (e.g.
    # commit_once skipped on quiescence and left the submodule dirty). The staged
    # check below is what makes that a no-op instead of an empty commit; verified.
    if not status or "M" not in status[:2]:
        logger.info("[selfedit-commit] parent pointer unchanged")
        return None
    _git_in(parent, "add", "--", sub)
    if not _git_in(parent, "diff", "--cached", "--name-only").stdout.strip():
        return None
    head = _git("rev-parse", "--short", "HEAD").stdout.strip()
    subject = _git("log", "-1", "--format=%s").stdout.strip()
    msg = NEWLINE.join([
        f"chore: bump {sub} to {head}",
        "",
        "Pointer bump for self-edits committed inside the submodule.",
        f"Submodule HEAD: {subject}",
        "",
        _TRAILER,
        "",
    ])
    _git_in(parent, "commit", "-F", "-", stdin=msg)
    sha = _git_in(parent, "rev-parse", "--short", "HEAD").stdout.strip()
    logger.info("[selfedit-commit] bumped parent pointer as %s", sha)
    return sha

GH_TIMEOUT = 60
MERGE_WAIT_SECONDS = 10 * 60


def _gh(*args: str, repo: Path = REPO) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=str(repo), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=GH_TIMEOUT, check=True)


def _repo_slug(repo: Path = REPO) -> str:
    return _gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner", repo=repo).stdout.strip()


def publish(repo: Path = REPO) -> str | None:
    """Get local main's self-edit commits onto origin/main through a PR, then resync.

    Returns the merged PR URL, or None when there was nothing to publish.
    Raises on failure (run_loop alerts); a failed rebase is aborted first.
    """
    if _git_in(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != "main":
        logger.warning("[selfedit-commit] HEAD is not main -- not publishing")
        return None
    if _mid_operation(repo):
        return None
    _git_in(repo, "fetch", "origin", "--prune")
    ahead = int(_git_in(repo, "rev-list", "--count", "origin/main..HEAD").stdout.strip() or 0)
    if ahead == 0:
        behind = int(_git_in(repo, "rev-list", "--count", "HEAD..origin/main").stdout.strip() or 0)
        if behind:
            _git_in(repo, "merge", "--ff-only", "origin/main")
            logger.info("[selfedit-commit] fast-forwarded main by %d", behind)
        return None
    try:
        _git_in(repo, "rebase", "--autostash", "origin/main")
    except subprocess.CalledProcessError:
        try:
            _git_in(repo, "rebase", "--abort")
        except subprocess.CalledProcessError:
            pass
        raise
    branch = f"agent/self-edits-{dt.date.today():%Y%m%d}"
    _git_in(repo, "push", "-f", "origin", f"HEAD:refs/heads/{branch}")
    files = _git_in(repo, "diff", "--name-only", "origin/main..HEAD").stdout.split()
    body = ("Nightly agent self-edits (dreams / skill_manage), auto-published by "
            "selfedit_commit_cron.publish().\n\n" + "\n".join(f"- {f}" for f in files[:60]))
    existing = _gh("pr", "list", "--head", branch, "--state", "open", "--json", "number",
                   "-q", ".[0].number", repo=repo).stdout.strip()
    if existing:
        num = existing
    else:
        url = _gh("pr", "create", "--base", "main", "--head", branch,
                  "--title", f"chore(self-edit): nightly agent self-edits {dt.date.today():%Y-%m-%d}",
                  "--body", body, repo=repo).stdout.strip()
        num = url.rstrip("/").rsplit("/", 1)[-1]
    slug = _repo_slug(repo)
    deadline = time.time() + MERGE_WAIT_SECONDS
    state = ""
    while time.time() < deadline:
        state = _gh("pr", "view", num, "--json", "mergeStateStatus", "-q", ".mergeStateStatus", repo=repo).stdout.strip()
        if state in ("CLEAN", "HAS_HOOKS"):
            break
        time.sleep(30)
    if state not in ("CLEAN", "HAS_HOOKS"):
        raise RuntimeError(f"self-edit PR #{num} not mergeable after wait (state={state})")
    _gh("api", "-X", "PUT", f"repos/{slug}/pulls/{num}/merge", "-f", "merge_method=squash", repo=repo)
    try:
        _git_in(repo, "push", "origin", "--delete", branch)
    except subprocess.CalledProcessError:
        pass
    _git_in(repo, "fetch", "origin", "--prune")
    if _git_in(repo, "diff", "--name-only", "HEAD", "origin/main").stdout.strip():
        raise RuntimeError(f"PR #{num} merged but local HEAD tree != origin/main; left local main as-is")
    _git_in(repo, "reset", "--mixed", "origin/main")  # keeps any newer on-disk edits as dirty files
    logger.info("[selfedit-commit] published self-edits via PR #%s; main == origin/main", num)
    return f"https://github.com/{slug}/pull/{num}"


def _seconds_until_next_run() -> float:
    now = dt.datetime.now()
    today_run = now.replace(hour=COMMIT_HOUR_LOCAL, minute=COMMIT_MINUTE_LOCAL,
                            second=0, microsecond=0)
    next_run = today_run if today_run > now else today_run + dt.timedelta(days=1)
    return max(60.0, (next_run - now).total_seconds())


def run_loop() -> None:
    """Entry point invoked by cron_supervisor."""
    logger.info("[selfedit-commit] cron started; repo=%s target=%02d:%02d local",
                REPO, COMMIT_HOUR_LOCAL, COMMIT_MINUTE_LOCAL)
    while True:
        time.sleep(_seconds_until_next_run())
        try:
            commit_once()
            publish(REPO)            # deer-flow first: its main sha becomes the pointer target
            bump_parent_pointer()
            publish(REPO.parent)     # then the parent repo (redshift-bot) pointer bump
        except subprocess.CalledProcessError as e:
            logger.exception("[selfedit-commit] git failed")
            detail = (e.stderr or e.stdout or str(e))[-600:]
            _slack_alert(
                ":warning: *self-edit commit failed* -- the agent's own edits are "
                "still uncommitted in `deer-flow`.\n```" + detail + "```"
            )
        except Exception as e:
            logger.exception("[selfedit-commit] run failed")
            _slack_alert(f":warning: *self-edit commit failed*: `{e}`")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [selfedit-commit] %(message)s")
    if "--once" in sys.argv:
        result = commit_once()
        print(f"committed: {result}" if result else "nothing committed")
        bumped = bump_parent_pointer()
        print(f"parent bumped: {bumped}" if bumped else "parent pointer unchanged")
        if "--no-publish" not in sys.argv:
            print(f"published deer-flow: {publish(REPO)}")
            print(f"published parent: {publish(REPO.parent)}")
    else:
        run_loop()
