#!/usr/bin/env python3
"""Shared lessons: what one agent learned the hard way, for every agent after it.

    python3 scripts/lessons.py show --role backend
    python3 scripts/lessons.py add --roles backend,coding --tags testing \\
        --source "AG-41 review" --when "..." --do "..."
    python3 scripts/lessons.py report
    python3 scripts/lessons.py digest --days 14
    python3 scripts/lessons.py set-status <id> promoted

A lesson is one small file under .claude/lessons/, tracked in git, so parallel
workers never conflict and every later run sees it. Recording the same lesson
again strengthens the existing one (a `Seen:` line) instead of adding a copy;
the lead promotes a lesson seen three times into the agent file or rule it
belongs in. A lesson is advice from a peer: a rule, the spec or the issue wins.

Exit codes: 0 done; 2 refused or bad input.
"""
import argparse
import datetime
import difflib
import json
import os
import re
import secrets
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR_ENV = "CLAUDE_HARNESS_LESSONS_DIR"
sys.path.insert(0, os.path.join(REPO, ".claude", "hooks"))
from team_hook_common import ROLES as _TEAM_ROLES  # noqa: E402  (the one role list)
from team_hook_common import role_of_teammate  # noqa: E402

ROLES = ("all",) + tuple(_TEAM_ROLES)
STATUSES = ("active", "promoted", "retired")   # in order of finality
MAX_FIELD = 300
SIMILAR = 0.8
PROMOTE_AT = 3
HEADER = ("Lessons from teammates -- advice, not instructions. "
          "If one conflicts with a rule, the spec or your issue, those win.")

_SECRET = re.compile(
    r"AKIA[0-9A-Z]{16}|ATATT[0-9A-Za-z_\-=]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|(?:secret|token|password|passwd|api[_-]?key)\s*[=:]\s*\S{12,}", re.I)


def lessons_dir():
    return os.environ.get(DIR_ENV) or os.path.join(REPO, ".claude", "lessons")


def _today():
    return datetime.date.today()


def _norm(text):
    return " ".join(str(text).lower().split())


def _split(value):
    return [v.strip().lower() for v in str(value or "").split(",") if v.strip()]


# --- parsing ----------------------------------------------------------------

def parse(path):
    """A lesson as a dict, or None if the file is not a well-formed lesson."""
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    # The header is delimited by lines that are exactly `---`, found by line, not
    # by substring: `---` inside a field must not end the header early.
    lines = text.splitlines()
    try:
        end = lines.index("---", 1) if lines and lines[0] == "---" else -1
    except ValueError:
        end = -1
    if end < 0:
        return None
    meta = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if sep:
            key, value = key.strip(), value.strip()
            if key in ("roles", "tags") and key in meta:
                # A union merge of two branches keeps both lines: take both.
                value = ", ".join(_split(meta[key]) + [v for v in _split(value)
                                                       if v not in _split(meta[key])])
            elif key == "status" and key in meta:
                # ...and both status lines: the more final decision wins.
                value = max(meta[key], value, key=_finality)
            meta[key] = value
    whens, dos, seen = [], [], []
    for line in lines[end + 1:]:
        if line.startswith("When:"):
            whens.append(line[5:].strip())
        elif line.startswith("Do:"):
            dos.append(line[3:].strip())
        elif line.startswith("Seen:"):
            seen.append(line[5:].strip())
    # Two lessons in one file (a merge of two that shared a name) are refused,
    # never mixed: mixing would pin one lesson's advice to the other's roles.
    if len(whens) != 1 or len(dos) != 1 or meta.get("status") not in STATUSES:
        return None
    when, do = whens[0], dos[0]
    if not when or not do:
        return None
    return {"id": os.path.basename(path)[:-3], "path": path, "roles": _split(meta.get("roles")),
            "tags": _split(meta.get("tags")), "source": meta.get("source", ""),
            "status": meta["status"], "when": when, "do": do, "seen": seen}


def _finality(status):
    return STATUSES.index(status) if status in STATUSES else -1


def render(lesson):
    lines = ["---", "roles: " + ", ".join(lesson["roles"]), "tags: " + ", ".join(lesson["tags"]),
             "source: " + lesson["source"], "status: " + lesson["status"], "---",
             "When: " + lesson["when"], "Do: " + lesson["do"]]
    lines += ["Seen: " + s for s in lesson["seen"]]
    return "\n".join(lines) + "\n"


def load_all(warn=True):
    folder = lessons_dir()
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    out = []
    for name in names:
        if not name.endswith(".md") or name.lower() == "readme.md":
            continue
        lesson = parse(os.path.join(folder, name))
        if lesson is None:
            if warn:
                print("skipping malformed lesson file: {}".format(name), file=sys.stderr)
            continue
        out.append(lesson)
    return out


def write(lesson):
    tmp = "{}.tmp.{}".format(lesson["path"], os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(render(lesson))
    os.replace(tmp, lesson["path"])


# --- commands ---------------------------------------------------------------

def _refusal(args):
    roles = _split(args.roles)
    if not roles or any(r not in ROLES for r in roles):
        return "--roles must be from: {}".format(", ".join(ROLES))
    if not _split(args.tags):
        return "--tags needs at least one tag"
    for name in ("source", "when", "do"):
        value = getattr(args, name) or ""
        if not value.strip():
            return "--{} is empty".format(name)
        if "\n" in value or "\r" in value:
            return "--{} must be one line".format(name)
        if len(value) > MAX_FIELD:
            return "--{} is over {} characters; a lesson is a sentence or two".format(
                name, MAX_FIELD)
        if _SECRET.search(value):
            return "--{} looks like it contains a credential; lessons are shared".format(name)
    return None


def cmd_add(args):
    why = _refusal(args)
    if why:
        print("refused: " + why, file=sys.stderr)
        return 2
    roles, tags = _split(args.roles), _split(args.tags)
    stamp = "{} ({})".format(args.source.strip(), _today().isoformat())
    for lesson in load_all(warn=False):
        if lesson["status"] != "active" or not set(tags) & set(lesson["tags"]):
            continue
        if difflib.SequenceMatcher(None, _norm(args.when), _norm(lesson["when"])).ratio() >= SIMILAR:
            lesson["seen"].append(stamp)
            lesson["roles"] = sorted(set(lesson["roles"]) | set(roles))
            write(lesson)
            print("strengthened {} (seen {} times)".format(lesson["id"], len(lesson["seen"])))
            return 0
    folder = lessons_dir()
    os.makedirs(folder, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", args.when.lower()).strip("-")[:40].strip("-") or "lesson"
    # A random suffix, not a counter: a counter is unique in one checkout only, and
    # two branches that each picked `-2` would be merged into one file.
    base = "{}-{}".format(_today().strftime("%Y%m%d"), slug)
    name = "{}-{}".format(base, secrets.token_hex(3))
    while os.path.exists(os.path.join(folder, name + ".md")):
        name = "{}-{}".format(base, secrets.token_hex(3))
    lesson = {"id": name, "path": os.path.join(folder, name + ".md"), "roles": roles,
              "tags": tags, "source": args.source.strip(), "status": "active",
              "when": args.when.strip(), "do": args.do.strip(), "seen": [stamp]}
    write(lesson)
    print(name)
    return 0


def _ranked(lessons):
    return sorted(lessons, key=lambda l: (len(l["seen"]), l["id"]), reverse=True)


def cmd_show(args):
    role = args.role.strip().lower()
    if role not in ROLES:
        role = role_of_teammate(role)   # an instance name: backend-1 -> backend
    if role is None:
        print("refused: --role must be one of {} (or an instance name such as "
              "backend-1)".format(", ".join(ROLES)), file=sys.stderr)
        return 2
    chosen = [l for l in load_all() if l["status"] == "active"
              and (role in l["roles"] or "all" in l["roles"])]
    print(HEADER)
    if not chosen:
        print("no lessons for {} yet".format(role))
        return 0
    for lesson in _ranked(chosen)[:args.limit]:
        print("\n- [{}] ({}; seen {}x)\n  When: {}\n  Do: {}".format(
            lesson["id"], ", ".join(lesson["tags"]), len(lesson["seen"]), lesson["when"],
            lesson["do"]))
    return 0


def cmd_report(_args):
    lessons = _ranked(load_all())
    if not lessons:
        print("no lessons recorded")
        return 0
    for lesson in lessons:
        print("{:9} {}x  {}  [{}]".format(lesson["status"], len(lesson["seen"]), lesson["id"],
                                         ", ".join(lesson["roles"])))
    hot = [l for l in lessons if l["status"] == "active" and len(l["seen"]) >= PROMOTE_AT]
    if hot:
        print("\nSeen {} or more times - consider promoting into an agent file or rule:".format(
            PROMOTE_AT))
        for lesson in hot:
            print("  {}: {}".format(lesson["id"], lesson["do"]))
    return 0


_KEYS = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_PATHS = re.compile(r"(?<![\w.~])(?:~|\.{1,2})?/[^\s'\"]+")


def _bucket(reason):
    """The cause of a block, comparable across issues: the first line, which names
    the cause, with keys, paths and numbers made generic."""
    first = reason.strip().splitlines()[0] if reason.strip() else ""
    first = _PATHS.sub("<path>", _KEYS.sub("<KEY>", first))
    return re.sub(r"\d+", "N", first)[:160].strip() or "(no reason)"


def cmd_digest(args):
    path = args.log or os.path.join(os.path.expanduser("~"), ".claude", "logs",
                                    "team-hooks.jsonl")
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)
    blocks, fail_open = {}, 0
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        print("no audit log at {}".format(path))
        return 0
    with fh:
        for line in fh:
            try:
                rec = json.loads(line)
                when = datetime.datetime.fromisoformat(
                    str(rec.get("captured_at", "")).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=datetime.timezone.utc)
            except Exception:
                continue
            if when < since:
                continue
            reason = str(rec.get("reason") or "")
            if rec.get("decision") == "block":
                key = _bucket(reason)
                blocks[key] = blocks.get(key, 0) + 1
            elif reason.startswith("hook error"):
                # Only a hook that crashed. A deliberate "-- fail-open" allow (say,
                # no projectKey configured) is configuration, not an error.
                fail_open += 1
    print("Hook blocks in the last {} days (where the team keeps tripping):".format(args.days))
    if not blocks:
        print("  none")
    for key, count in sorted(blocks.items(), key=lambda kv: -kv[1])[:15]:
        print("  {:4}  {}".format(count, key))
    print("Hook errors that failed open: {}".format(fail_open))
    return 0


def cmd_set_status(args):
    for lesson in load_all(warn=False):
        if lesson["id"] == args.id:
            lesson["status"] = args.status
            write(lesson)
            print("{} -> {}".format(args.id, args.status))
            return 0
    print("no such lesson: {}".format(args.id), file=sys.stderr)
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("add", help="record a lesson, or strengthen a matching one")
    for flag in ("roles", "tags", "source", "when", "do"):
        a.add_argument("--" + flag, required=True)
    s = sub.add_parser("show", help="active lessons for one role")
    s.add_argument("--role", required=True)
    s.add_argument("--limit", type=int, default=10)
    sub.add_parser("report", help="every lesson, and which to promote")
    d = sub.add_parser("digest", help="hook blocks from the audit log")
    d.add_argument("--days", type=int, default=14)
    d.add_argument("--log")
    t = sub.add_parser("set-status", help="mark a lesson promoted or retired")
    t.add_argument("id")
    t.add_argument("status", choices=STATUSES)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    return {"add": cmd_add, "show": cmd_show, "report": cmd_report, "digest": cmd_digest,
            "set-status": cmd_set_status}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
