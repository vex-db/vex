#!/usr/bin/env python3
"""Regenerate the GitHub wiki from the canonical docs/ tree.

The wiki is a published mirror of a curated subset of docs/. Maintaining two
hand-edited copies is what let the wiki rot to a pre-agent-memory snapshot;
this script makes the wiki a pure function of docs/ so it can't drift again.

Usage: tools/sync-wiki.py [WIKI_DIR]   (default: ../vex.wiki, sibling of repo)
       tools/sync-wiki.py --selfcheck  (run link-rewrite asserts, touch nothing)

GitHub wiki pages are flat (no dirs), so docs/foo-bar.md -> Foo-Bar.md and
links like (foo-bar.md) -> (Foo-Bar). README.md becomes Home. Anything not in
the mirrored set (CHANGELOG, design sketches) links out to the repo blob.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BLOB = "https://github.com/vex-db/vex/blob/main"

# Curated public surface, by docs/ basename (README -> Home). Edit when a
# doc should start/stop appearing in the wiki.
PAGES = [
    "agent-memory", "semantic-cache", "graphrag", "vector-search",
    "commands", "architecture", "benchmarks", "tuning",
    "configuration", "deployment", "persistence", "security", "clustering",
    "transactions", "pubsub", "observability", "roadmap", "testing",
    "usage-patterns",
]
LINK = re.compile(r"\]\(([^)]+)\)")


def page_name(base):
    """docs basename -> wiki page name. README is the wiki Home."""
    if base == "README":
        return "Home"
    return "-".join(p.capitalize() for p in base.split("-"))


def rewrite_link(target):
    """Rewrite one markdown link target for the wiki."""
    if "://" in target or target.startswith("#"):
        return target  # external or in-page anchor
    path, _, anchor = target.partition("#")
    anchor = f"#{anchor}" if anchor else ""
    if not path.endswith(".md"):
        return target  # image, asset, etc.
    base = Path(path).stem
    if base == "README" or base in PAGES:
        return page_name(base) + anchor
    # Real doc that isn't a wiki page -> link out to the repo.
    return f"{BLOB}/{path.lstrip('./').replace('../', '')}{anchor}"


def convert(text):
    return LINK.sub(lambda m: "](" + rewrite_link(m.group(1)) + ")", text)


def selfcheck():
    assert rewrite_link("../README.md") == "Home"
    assert rewrite_link("commands.md") == "Commands"
    assert rewrite_link("agent-memory.md#recall") == "Agent-Memory#recall"
    assert rewrite_link("docs/pubsub.md") == "Pubsub"
    assert rewrite_link("../CHANGELOG.md") == f"{BLOB}/CHANGELOG.md"
    assert rewrite_link("docs/af-xdp-design.md") == f"{BLOB}/docs/af-xdp-design.md"
    assert rewrite_link("https://x.com") == "https://x.com"
    print("selfcheck ok")


def main(wiki_dir):
    wiki = Path(wiki_dir).resolve()
    if not (wiki / ".git").is_dir():
        sys.exit(f"not a wiki clone: {wiki}")

    srcs = {"Home": REPO / "README.md"}
    for base in PAGES:
        srcs[page_name(base)] = REPO / "docs" / f"{base}.md"

    missing = [str(p) for p in srcs.values() if not p.exists()]
    if missing:
        sys.exit("missing source docs:\n  " + "\n  ".join(missing))

    for old in wiki.glob("*.md"):  # clean slate -> drops stale-named pages
        old.unlink()
    for name, src in srcs.items():
        (wiki / f"{name}.md").write_text(convert(src.read_text()))
    print(f"wrote {len(srcs)} pages to {wiki}")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else REPO.parent / "vex.wiki")
