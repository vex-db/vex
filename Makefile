# Vex Makefile — convenience wrappers around zig build + release housekeeping.
# Most day-to-day work goes through `zig build` directly; this file exists
# so release bumps and CI checks have a single canonical entry point.

# ── Canonical version ─────────────────────────────────────────────────
# Single source of truth lives in build.zig.zon; build.zig propagates it
# to the binary via a build option. The Makefile reads the same field so
# `sync-docs` can rewrite human-facing prose to match a release bump.
VERSION := $(shell awk -F'"' '/^[[:space:]]*\.version[[:space:]]*=/{print $$2; exit}' build.zig.zon)

# Docs that carry a "current example" version string that must track each
# release. Each one is rewritten by `sync-docs` and checked by `check-docs`.
# CHANGELOG-style mentions (e.g. README.md "### v0.7.1") are intentionally
# excluded — they are historical records, not current pointers.
VERSIONED_DOCS := docs/deployment.md docs/observability.md

.PHONY: help version sync-docs check-docs check-docs-strict

help:
	@echo "Vex Makefile targets:"
	@echo "  make version           — print the canonical version from build.zig.zon"
	@echo "  make sync-docs         — rewrite \"current example\" version mentions in $(VERSIONED_DOCS)"
	@echo "  make check-docs        — exit 1 if any versioned doc disagrees with build.zig.zon"
	@echo "  make check-docs-strict — same as check-docs, plus warns on any other 0.x.y string in those files"
	@echo ""
	@echo "Build, run, test, bench: use 'zig build' (see build.zig for steps)."

version:
	@echo "$(VERSION)"

# In-place rewrite. Touches:
#   - "ghcr.io/pratyush-sngh/vex:X.Y.Z" specific-tag lines
#   - "vex_version:X.Y.Z" INFO-output examples
#   - "vex vX.Y.Z ready" startup-log examples
# These are the only "current example" patterns documented today. Add new
# patterns as new doc examples are written; keep the deletion side small.
sync-docs:
	@if [ -z "$(VERSION)" ]; then \
		echo "ERR: could not read .version from build.zig.zon"; exit 1; \
	fi
	@for f in $(VERSIONED_DOCS); do \
		sed -i.bak -E \
			-e 's@(ghcr\.io/pratyush-sngh/vex:)[0-9]+\.[0-9]+\.[0-9]+@\1$(VERSION)@g' \
			-e 's@(vex_version:)[0-9]+\.[0-9]+\.[0-9]+@\1$(VERSION)@g' \
			-e 's@(vex v)[0-9]+\.[0-9]+\.[0-9]+( ready)@\1$(VERSION)\2@g' \
			"$$f" && rm "$$f.bak"; \
	done
	@echo "synced $(VERSIONED_DOCS) → $(VERSION)"

# Read-only check. Suitable for CI: exits 1 if any tracked doc references
# a version that is not the canonical one.
check-docs:
	@if [ -z "$(VERSION)" ]; then \
		echo "ERR: could not read .version from build.zig.zon"; exit 1; \
	fi
	@stale=0; \
	for f in $(VERSIONED_DOCS); do \
		bad=$$(grep -nE \
			-e "ghcr\.io/pratyush-sngh/vex:[0-9]+\.[0-9]+\.[0-9]+" \
			-e "vex_version:[0-9]+\.[0-9]+\.[0-9]+" \
			-e "vex v[0-9]+\.[0-9]+\.[0-9]+ ready" "$$f" \
			| grep -vE "(:$(VERSION)|v$(VERSION) )" || true); \
		if [ -n "$$bad" ]; then \
			echo "$$f: stale version mentions (canonical: $(VERSION)):"; \
			echo "$$bad" | sed 's/^/    /'; \
			stale=1; \
		fi; \
	done; \
	if [ $$stale -eq 1 ]; then \
		echo "run 'make sync-docs' to fix"; \
		exit 1; \
	fi; \
	echo "docs in sync with $(VERSION)"

# Same as check-docs, but ALSO flags any other 0.x.y in the versioned
# docs. Use when adding new doc content — catches embedded version
# strings that the regex set above doesn't know about yet so they can
# be added to sync-docs.
check-docs-strict: check-docs
	@for f in $(VERSIONED_DOCS); do \
		others=$$(grep -nE "[0-9]+\.[0-9]+\.[0-9]+" "$$f" \
			| grep -vE "[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+" \
			| grep -vE "(ghcr\.io/pratyush-sngh/vex:[0-9]+|vex_version:[0-9]+|vex v[0-9]+)" \
			| grep -vE "minimum_zig_version|zig 0\." || true); \
		if [ -n "$$others" ]; then \
			echo "WARN: $$f has other version-like strings not covered by sync-docs:"; \
			echo "$$others" | sed 's/^/    /'; \
		fi; \
	done
