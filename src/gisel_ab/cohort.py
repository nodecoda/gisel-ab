#!/usr/bin/env python3
"""One cohort, from tag to a re-checkable record.

    cohort.py plan [--online] [--json]
    cohort.py tag NAME [--push]
    cohort.py verify --clone DIR [--out DIR] [--only NAME,...] [--jobs N]
    cohort.py record-ci --file CI.json
    cohort.py check [--worktree] [--json]
    cohort.py diff NEW OLD [--json] [--root DIR]

A cohort is three commits (gisel, gisel-anchor, gisel-ab) and a set of gates.
`RELEASE.json` names the commits and the gate commands; this file adds what
the manifest deliberately does not carry: which repo each gate runs in, which
files are its inputs, and a fingerprint over those files.

`verify` runs the gates in clean clones at the tags and writes a draft
record.  `check` proves that a record still matches the commits it names;
`check --worktree` compares it with the current worktrees instead, which is
how a changed corpus shows up as a STALE gate before anyone tags again.

The record commit is a child of the cohort tag, so at the tag itself the
checked-in VERIFICATION.json still names the previous cohort.  `check` skips
that state with rc=0 and says so; only a record that names the current
cohort is evidence about the current cohort.

Exit codes: 0 = ok; 1 = a gate failed / a fingerprint is stale; 2 = usage,
missing manifest, dirty tree, or a tag that already exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from gisel_ab import paths

PKG = pathlib.Path(__file__).resolve().parent
AB = PKG.parents[1]
GISEL = pathlib.Path(os.environ.get("GISEL_SRC", AB.parent / "gisel"))
UPSTREAM = paths.sibling(AB.parent, "UPSTREAM_SOURCE",
                         "gisel-anchor", "upstream-source")
MANIFEST = AB / "RELEASE.json"
VERIFICATION = AB / "VERIFICATION.json"

# A gate command from RELEASE.json -> where it runs and what it reads.
# Cross-repo inputs are written "repo:pathspec"; pathspecs are passed to
# `git ls-tree` / `git ls-files`, so globs and directories both work.
VERIFY_EVIDENCE = {
    "repo": "ab",
    "command": "./scripts/verify-evidence.sh",
    "inputs": ["EVIDENCE.json", "src/gisel_ab/evidence.py",
               "evidence.schema.json", "evidence/**/*",
               "src/gisel_ab/data/**/*", "scripts/**/*",
               "gisel:tests/artifacts/**/*"],
}

# r7 and earlier spelled every workbench path from the repository root.  The
# productization moved code under src/ and scripts/; the old spellings stay
# here so an old cohort can still be replayed against its own layout.
VERIFY_EVIDENCE_ROOTED = {
    "repo": "ab",
    "command": "./verify-evidence.sh",
    "inputs": ["EVIDENCE.json", "evidence.py", "evidence.schema.json",
               "oracle/**/*", "data/**/*", "*.sh", "gisel:tests/artifacts/**/*"],
}

GATES = {
    "python3 -m unittest discover -q -s tests": {
        "repo": "gisel",
        "inputs": ["gisel/*.py", "gisel/**/*.py", "tests/**/*", "spec/**/*"],
    },
    "bash tools/check-nine-corpora.sh": {
        "repo": "gisel",
        "inputs": ["tests/corpus/*.gisel", "tests/artifacts/**/*",
                   "gisel/**/*.py", "tools/**/*"],
    },
    "bash tools/check-graph-golden.sh": {
        "repo": "gisel",
        "inputs": ["tests/corpus/*.gisel", "tests/cases/*.json",
                   "tests/artifacts/graph/**/*", "gisel/**/*.py", "tools/**/*",
                   "spec/graph.md"],
    },
    "bash tools/check-cases-golden.sh": {
        "repo": "gisel",
        "inputs": ["tests/corpus/*.gisel", "tests/cases/*.json",
                   "tests/artifacts/cases/**/*", "gisel/**/*.py", "tools/**/*",
                   "spec/cases-format.md"],
    },
    "bash tools/check-nightly-graphs.sh": {
        "repo": "gisel",
        "inputs": ["tests/corpus/*.gisel", "tests/cases/*.json",
                   "tests/artifacts/graph/**/*", "gisel/**/*.py", "tools/**/*"],
        "slow": True,
    },
    "python3 tools/check-claims.py": {
        "repo": "gisel",
        "inputs": ["docs/**/*.md", "gisel/**/*.py", "tools/**/*", "spec/**/*"],
    },
    "python3 importer/import-cxx.py --all --pin pin/PIN.json --check": {
        "repo": "upstream_source",
        "inputs": ["importer/**/*", "pin/**/*", "oracle/**/*",
                   "gisel:tests/corpus/*.gisel"],
        "expand": "import-check",
    },
    "pin/make-base.sh TREE DEST --verify": {
        "repo": "upstream_source",
        "inputs": ["pin/**/*"],
        "expand": "make-base",
    },
    # r7 moved the anchor's entry points into scripts/ (the code itself lives
    # under src/gisel_anchor).  The r6 spellings above stay for replaying r6.
    "python3 scripts/import-cxx.py --all --pin pin/PIN.json --check": {
        "repo": "upstream_source",
        "inputs": ["src/**/*", "scripts/**/*", "pin/**/*",
                   "gisel:tests/corpus/*.gisel"],
        "expand": "import-check",
    },
    "scripts/pin/make-base.sh TREE DEST --verify": {
        "repo": "upstream_source",
        "inputs": ["pin/**/*", "scripts/pin/**/*"],
        "expand": "make-base",
    },
    # r5 names the harness by its repository name; r4 and earlier manifests
    # said `ab/verify-evidence.sh`.  Both point at the same gate.
    "gisel-ab/scripts/verify-evidence.sh": VERIFY_EVIDENCE,
    "gisel-ab/verify-evidence.sh": VERIFY_EVIDENCE_ROOTED,
    "ab/verify-evidence.sh": VERIFY_EVIDENCE_ROOTED,
}

# Gates the workbench runs on itself but does not put in RELEASE.json: the
# manifest names product evidence, not the harness's own unit suite.
WORKBENCH_GATES = {
    "python3 -m pytest tests -q": {
        "repo": "ab",
        "inputs": ["tests/**/*", "src/**/*", "scripts/**/*", "evidence/**/*"],
    },
}

ONLINE_GATES = {
    "adopt-check": {
        "repo": "ab",
        "command": "./scripts/adopt-check.sh",
        "inputs": ["EVIDENCE.json", "src/gisel_ab/evidence.py",
                   "gisel:tests/artifacts/*.inc"],
        "online": True,
    },
}

REPO_DIRS = {"gisel": GISEL, "upstream_source": UPSTREAM, "ab": AB}


def fail(message, rc=2):
    print("cohort: %s" % message, file=sys.stderr)
    return rc


def git(repo, *args, input_text=None):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, input=input_text)


def head(repo):
    found = git(repo, "rev-parse", "HEAD")
    if found.returncode:
        raise SystemExit("cohort: %s is not a git checkout: %s"
                         % (repo, found.stderr.strip()))
    return found.stdout.strip()


def dirty(repo):
    found = git(repo, "status", "--porcelain")
    return bool(found.stdout.strip())


def load_manifest():
    if not MANIFEST.is_file():
        raise SystemExit("cohort: missing %s" % MANIFEST)
    return json.loads(MANIFEST.read_text())


def split_input(spec):
    if ":" in spec:
        repo, path = spec.split(":", 1)
        return repo, path
    return None, spec


def gate_table(manifest, online=False):
    """RELEASE gates plus the workbench's extra checks, with their inputs."""
    table = []
    for command in manifest["gates"]:
        if command not in GATES:
            raise SystemExit("cohort: no descriptor for gate %r; add it to "
                             "cohort.py GATES" % command)
        spec = GATES[command]
        table.append({
            "name": spec.get("command", command),
            "manifest_command": command,
            "repo": spec["repo"],
            "command": spec.get("command", command),
            "inputs": spec["inputs"],
            "slow": bool(spec.get("slow")),
            "online": False,
            "expand": spec.get("expand"),
        })
    for command, spec in WORKBENCH_GATES.items():
        table.append({
            "name": command,
            "manifest_command": None,
            "repo": spec["repo"],
            "command": command,
            "inputs": spec["inputs"],
            "slow": False,
            "online": False,
            "expand": None,
        })
    if online:
        for name, spec in ONLINE_GATES.items():
            table.append({
                "name": name,
                "manifest_command": None,
                "repo": spec["repo"],
                "command": spec["command"],
                "inputs": spec["inputs"],
                "slow": False,
                "online": True,
                "expand": None,
            })
    return table


def _repo_path(name):
    if name == "gisel":
        return GISEL
    if name == "upstream_source":
        return UPSTREAM
    if name == "ab":
        return AB
    raise SystemExit("cohort: unknown repo %r" % name)


_CACHE = {}


def _matches(path, spec):
    """git pathspec-ish match, with ** meaning "any depth, including zero".

    ls-tree does not expand `*`, so the listing is done once per revision and
    filtered here; fnmatch treats `*` as matching `/` too, which is the
    conservative direction for an input fingerprint.
    """
    import fnmatch
    if fnmatch.fnmatchcase(path, spec):
        return True
    if "/**/" in spec:
        return fnmatch.fnmatchcase(path, spec.replace("/**/", "/"))
    return False


def _committed_map(repo, rev):
    key = ("commit", str(repo), rev)
    if key in _CACHE:
        return _CACHE[key]
    found = git(repo, "ls-tree", "-r", "-z", rev)
    if found.returncode:
        raise SystemExit("cohort: ls-tree %s failed: %s"
                         % (repo, found.stderr.strip()))
    rows = {}
    for record in found.stdout.split("\0"):
        if not record:
            continue
        meta, path = record.split("\t", 1)
        fields = meta.split()
        if len(fields) == 3 and fields[1] == "blob":
            rows[path] = fields[2]
    _CACHE[key] = rows
    return rows


def _worktree_map(repo):
    key = ("worktree", str(repo))
    if key in _CACHE:
        return _CACHE[key]
    found = git(repo, "ls-files", "-z", "--cached", "--others",
                "--exclude-standard")
    if found.returncode:
        raise SystemExit("cohort: ls-files %s failed: %s"
                         % (repo, found.stderr.strip()))
    paths = sorted({p for p in found.stdout.split("\0") if p})
    blobs = {}
    if paths:
        hashed = git(repo, "hash-object", "--stdin-paths",
                     input_text="\n".join(str(repo / p) for p in paths) + "\n")
        if hashed.returncode:
            raise SystemExit("cohort: hash-object failed in %s: %s"
                             % (repo, hashed.stderr.strip()))
        digests = hashed.stdout.split()
        if len(digests) != len(paths):
            raise SystemExit("cohort: hash-object returned %d digests for %d "
                             "paths" % (len(digests), len(paths)))
        blobs = dict(zip(paths, digests))
    _CACHE[key] = blobs
    return blobs


def _select(blobs, spec):
    return {path: blob for path, blob in blobs.items() if _matches(path, spec)}


def _blob_map(gate, commits=None, worktree=False):
    rows = {}
    for spec in gate["inputs"]:
        repo_name, pathspec = split_input(spec)
        if repo_name is None:
            repo_name = gate["repo"]
        repo = _repo_path(repo_name)
        source = (_worktree_map(repo) if worktree
                  else _committed_map(repo, commits[repo_name]))
        for path, blob in _select(source, pathspec).items():
            rows["%s:%s" % (repo_name, path)] = blob
    return rows


def fingerprint(gate, commits=None, worktree=False):
    """sha256 over (path, blob) for one gate's inputs.

    With `commits`, read the committed blobs at those revs; with `worktree`,
    hash the files on disk (tracked and untracked).  Both sides are git blob
    ids of file content, so the two modes are comparable for a clean tree.
    """
    rows = _blob_map(gate, commits=commits, worktree=worktree)
    lines = ["%s\0%s\n" % (path, blob) for path, blob in sorted(rows.items())]
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def changed_inputs(gate, record, worktree=True):
    """Files whose blob differs between the record and a checkout."""
    expected = _blob_map(gate, commits=record.get("gate_commits",
                                                  record["commits"]),
                         worktree=worktree)
    stored = None
    for item in record["gates"]:
        if item.get("name") == gate["name"]:
            stored = item.get("blobs")
    if stored is None:
        return sorted(expected)[:5]
    changed = [key for key, blob in expected.items() if stored.get(key) != blob]
    return sorted(changed)[:5]


def _expand_command(gate, clone_root, out_dir):
    command = gate["command"]
    if gate.get("expand") == "make-base":
        tree = paths.require_tree()
        dest = pathlib.Path(out_dir) / "pin-base"
        if dest.exists():
            shutil.rmtree(dest)
        command = gate["command"].replace("TREE", str(tree)).replace(
            "DEST", str(dest))
    elif gate.get("expand") == "import-check":
        llvm = paths.llvm_repo(
            fallback=os.environ.get("COHORT_TREE")
            or os.environ.get("AB_TREE"))
        command = command + " --git-repo %s" % llvm
    return command


def _last_line(text, limit=200):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    line = lines[-1]
    return line if len(line) <= limit else line[:limit - 3] + "..."


def _run_one(gate, clones, out_dir, timeout):
    repo = pathlib.Path(clones) / gate["repo"]
    try:
        command = _expand_command(gate, clones, out_dir)
    except SystemExit as exc:
        # A missing LLVM checkout is this gate's input error, not a reason to
        # abandon the whole cohort: report it as rc=2 so the draft says 9/10
        # with a named failure instead of looking like a short, green run.
        return {
            "name": gate["name"],
            "repo": gate["repo"],
            "manifest_command": gate.get("manifest_command"),
            "command": gate["command"],
            "rc": 2,
            "evidence": "input error: missing LLVM root (rc=%s)"
                        % (exc.code if exc.code is not None else 2),
            "duration_s": 0.0,
        }
    env = dict(os.environ)
    env.update({
        "GISEL_SRC": str(pathlib.Path(clones) / "gisel"),
        "AB_SRC": str(pathlib.Path(clones) / "ab"),
        "UPSTREAM_SOURCE": str(pathlib.Path(clones) / "upstream_source"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    if "verify-evidence.sh" in gate["name"]:
        env["VERIFY_FROZEN"] = "1"
    started = time.time()
    try:
        found = subprocess.run(["bash", "-lc", command], cwd=str(repo),
                               capture_output=True, text=True,
                               env=env, timeout=timeout)
        rc, stdout, stderr = found.returncode, found.stdout, found.stderr
    except subprocess.TimeoutExpired as exc:
        rc = 124
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = "timeout after %ss" % timeout
    elapsed = round(time.time() - started, 1)
    if gate.get("expand") == "make-base":
        tree = paths.require_tree()
        dest = pathlib.Path(out_dir) / "pin-base"
        subprocess.run(["git", "-C", tree, "worktree", "remove", "--force",
                        str(dest)], capture_output=True, text=True)
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
    log = pathlib.Path(out_dir) / ("%s.log" % gate["name"].replace("/", "_"))
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("$ %s\n\n[stdout]\n%s\n[stderr]\n%s\n"
                   % (command, stdout, stderr))
    return {
        "name": gate["name"],
        "repo": gate["repo"],
        "manifest_command": gate.get("manifest_command"),
        "command": command,
        "rc": rc,
        "evidence": _last_line(stdout) or _last_line(stderr),
        "duration_s": elapsed,
        "log": str(log),
    }


def cmd_plan(args):
    manifest = load_manifest()
    table = gate_table(manifest, online=args.online)
    rows = []
    for gate in table:
        rows.append({
            "name": gate["name"],
            "repo": gate["repo"],
            "command": gate["command"],
            "inputs": gate["inputs"],
            "slow": gate["slow"],
            "online": gate["online"],
        })
    if args.json:
        print(json.dumps({"cohort": manifest["cohort"], "gates": rows},
                         indent=2, sort_keys=True))
    else:
        print("cohort: %s" % manifest["cohort"])
        for gate in rows:
            flags = " slow" if gate["slow"] else ""
            flags += " online" if gate["online"] else ""
            print("  %-10s %-52s%s" % (gate["repo"], gate["name"], flags))
            print("             inputs: %s" % ", ".join(gate["inputs"]))
    return 0


def cmd_tag(args):
    manifest = load_manifest()
    if args.name != manifest["cohort"]:
        return fail("tag %r does not match RELEASE.json.cohort %r; regenerate "
                    "the manifest first" % (args.name, manifest["cohort"]))
    repos = {"gisel": GISEL, "upstream_source": UPSTREAM, "ab": AB}
    for name, repo in repos.items():
        if dirty(repo):
            return fail("%s has uncommitted changes; commit before tagging"
                        % name)
    if head(GISEL) != manifest["repos"]["gisel"]:
        return fail("gisel HEAD is not the manifest commit")
    if head(UPSTREAM) != manifest["repos"]["upstream_source"]:
        return fail("gisel-anchor HEAD is not the manifest commit")
    if head(AB) != manifest["repos"]["ab"]:
        manifest_edit = git(AB, "log", "-1", "--format=%H", "--", "RELEASE.json")
        if manifest_edit.stdout.strip() != head(AB):
            return fail("ab HEAD is neither repos.ab nor the manifest commit")
        parent = git(AB, "rev-parse", head(AB) + "^").stdout.strip()
        if parent != manifest["repos"]["ab"]:
            return fail("the manifest commit did not add only RELEASE.json")
    for name, repo in repos.items():
        existing = git(repo, "tag", "--list", args.name)
        if existing.stdout.strip():
            return fail("%s already has tag %s; do not rewrite it, use the "
                        "next -rN" % (name, args.name))
    for name, repo in repos.items():
        found = git(repo, "-c", "user.name=cohort",
                    "-c", "user.email=cohort@example.invalid",
                    "tag", "-a", args.name, "-m",
                    "cohort %s" % args.name, head(repo))
        if found.returncode:
            return fail("%s: %s" % (name, found.stderr.strip()))
        print("tagged %-16s %s" % (name, head(repo)))
    if args.push:
        for name, repo in repos.items():
            found = git(repo, "push", "origin", args.name)
            if found.returncode:
                return fail("%s: %s" % (name, found.stderr.strip()))
            print("pushed %-16s %s" % (name, args.name))
    return 0


def _clone(repo, dest, rev):
    found = git(repo, "clone", "-q", "--no-hardlinks", str(repo), str(dest))
    if found.returncode:
        return fail("clone %s: %s" % (repo, found.stderr.strip()))
    found = subprocess.run(["git", "-C", str(dest), "checkout", "-q", rev],
                           capture_output=True, text=True)
    if found.returncode:
        return fail("checkout %s in %s: %s"
                    % (rev, dest, found.stderr.strip()))
    return 0


def cmd_verify(args):
    manifest = load_manifest()
    table = gate_table(manifest)
    if args.only:
        wanted = {name.strip() for name in args.only.split(",")}
        table = [gate for gate in table if gate["name"] in wanted
                 or gate["command"] in wanted]
        missing = wanted - {gate["name"] for gate in table} - {gate["command"] for gate in table}
        if missing:
            return fail("unknown gate(s): %s" % ", ".join(sorted(missing)))
    clone_root = pathlib.Path(args.clone)
    clone_root.mkdir(parents=True, exist_ok=True)
    manifest_commit = git(AB, "log", "-1", "--format=%H", "--",
                          "RELEASE.json").stdout.strip()
    if not manifest_commit:
        return fail("RELEASE.json is not committed")
    # The manifest commit adds only RELEASE.json, but that file is an input
    # to the manifest tests; gate at the tag commit, not its parent.
    commits = dict(manifest["repos"])
    commits["ab"] = manifest_commit
    for name, rev in (("gisel", commits["gisel"]),
                      ("upstream_source", commits["upstream_source"]),
                      ("ab", commits["ab"])):
        dest = clone_root / name
        if not dest.exists() or args.reclone:
            if dest.exists():
                shutil.rmtree(dest)
            rc = _clone(_repo_path(name), dest, rev)
            if rc:
                return rc
        got = git(dest, "rev-parse", "HEAD").stdout.strip()
        if got != rev:
            return fail("%s clone is at %s, expected %s" % (name, got[:12], rev[:12]))
    out = pathlib.Path(args.out or (clone_root / "record"))
    out.mkdir(parents=True, exist_ok=True)
    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(_run_one, gate, clone_root, out,
                               args.timeout) for gate in table]
        for gate, future in zip(table, futures):
            result = future.result()
            result["fingerprint"] = fingerprint(gate, commits=commits)
            result["blobs"] = _blob_map(gate, commits=commits)
            results.append(result)
            print("%-6s %-10s %-48s %6.1fs  %s"
                  % ("ok" if result["rc"] == 0 else "FAIL",
                     result["repo"], result["name"],
                     result["duration_s"], result["evidence"]))
    payload = {
        "schema": 2,
        "cohort": manifest["cohort"],
        "tag": manifest["cohort"],
        "manifest_commit": manifest_commit,
        "tags": {name: manifest["cohort"]
                 for name in ("gisel", "upstream_source", "ab")},
        "commits": commits,
        "gate_commits": commits,
        "gates": results,
        "online_gates": [],
        "ci": {},
        "history": [],
        "recorded_at": time.strftime("%Y-%m-%d"),
    }
    draft = out / "verification-draft.json"
    if args.only and draft.exists():
        previous = json.loads(draft.read_text())
        fresh = {item["name"]: item for item in results}
        merged = []
        for item in previous.get("gates", []):
            merged.append(fresh.pop(item["name"], item))
        merged.extend(fresh.values())
        order = {gate["name"]: index for index, gate in enumerate(table)}
        payload["gates"] = sorted(merged, key=lambda item: order.get(item["name"], 999))
        payload["online_gates"] = previous.get("online_gates", [])
        payload["ci"] = previous.get("ci", {})
        payload["history"] = previous.get("history", [])
    draft.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    failed = [item for item in results if item["rc"] != 0]
    print("draft: %s (%d/%d gates rc=0)" % (draft, len(results) - len(failed),
                                            len(results)))
    return 1 if failed else 0


def cmd_record_ci(args):
    record = json.loads(VERIFICATION.read_text())
    update = json.loads(pathlib.Path(args.file).read_text())
    # `notes` explains the runs next to them; everything else is one entry
    # per repository (or `publish`).
    notes = update.pop("notes", None)
    if notes is not None:
        record["ci_notes"] = notes
    record.setdefault("ci", {}).update(update)
    tmp = VERIFICATION.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    tmp.replace(VERIFICATION)
    print("recorded ci: %s" % ", ".join(sorted(update)))
    return 0


def cmd_check(args):
    record = json.loads(VERIFICATION.read_text())
    if record.get("schema") != 2:
        return fail("VERIFICATION.json is schema %r; check needs schema 2"
                    % record.get("schema"))
    manifest = load_manifest()
    # The tag commit carries the previous cohort's record (the record commit
    # is its child).  Treat that as "not written yet", not as drift: there is
    # no record to compare against the manifest we are looking at.
    if record.get("cohort") != manifest["cohort"]:
        reason = ("record is %s, manifest is %s; the record commit lands "
                  "after the tag" % (record.get("cohort"), manifest["cohort"]))
        if args.worktree:
            return fail(reason + "; a worktree check needs a record for this "
                        "cohort")
        if args.json:
            print(json.dumps({"cohort": manifest["cohort"], "worktree": False,
                              "skipped": True, "reason": reason}, indent=2))
        else:
            print("cohort: %s; skipping" % reason)
        return 0
    table = gate_table(manifest)
    by_name = {gate["name"]: gate for gate in table}
    stale = []
    for item in record["gates"]:
        gate = by_name.get(item["name"])
        if gate is None:
            continue
        audit = record.get("gate_commits", record["commits"])
        got = fingerprint(gate, worktree=args.worktree,
                          commits=None if args.worktree else audit)
        if got != item["fingerprint"]:
            stale.append((gate, changed_inputs(gate, record,
                                               worktree=args.worktree)))
    rows = []
    for gate in table:
        item = next((x for x in record["gates"] if x["name"] == gate["name"]), None)
        rows.append({
            "name": gate["name"],
            "repo": gate["repo"],
            "state": "MISSING" if item is None else
                     ("STALE" if any(g is gate for g, _ in stale) else "FRESH"),
        })
    if args.json:
        print(json.dumps({"cohort": record["cohort"], "worktree": args.worktree,
                          "gates": rows,
                          "changed": {gate["name"]: files
                                      for gate, files in stale}}, indent=2))
    else:
        print("cohort: %s (%s)" % (record["cohort"],
                                   "worktree" if args.worktree else "tagged commits"))
        for row in rows:
            print("  %-6s %-10s %s" % (row["state"], row["repo"], row["name"]))
        for gate, files in stale:
            if files:
                print("  changed for %s: %s" % (gate["name"], ", ".join(files)))
    return 1 if stale else 0


class DiffError(Exception):
    """A cohort tag or a manifest is missing; not a difference."""


def _git_at(root, *args):
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True)


def _show_json(root, rev, name):
    found = _git_at(root, "show", "%s:%s" % (rev, name))
    if found.returncode:
        return None
    try:
        return json.loads(found.stdout)
    except ValueError:
        return None


def _record_for(root, cohort, limit=400):
    """The newest commit whose VERIFICATION.json names `cohort`."""
    found = _git_at(root, "log", "--format=%H", "-n", str(limit),
                    "--", "VERIFICATION.json")
    if found.returncode:
        return None
    for commit in found.stdout.split():
        record = _show_json(root, commit, "VERIFICATION.json")
        if record and record.get("cohort") == cohort:
            return commit
    return None


def _delta(old, new):
    old = old or {}
    new = new or {}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    return {
        "added": added,
        "removed": removed,
        "changed": {k: {"old": old[k], "new": new[k]} for k in changed},
        "count": len(added) + len(removed) + len(changed),
    }


def _evidence_index(evidence):
    index = {}
    for entry in (evidence or {}).get("entries", []):
        if "id" in entry:
            index[entry["id"]] = json.dumps(entry.get("artifacts") or {},
                                            sort_keys=True)
    return index


def _gate_index(record):
    return {gate["name"]: gate for gate in (record or {}).get("gates", [])}


def _blobs(record):
    out = {}
    for gate in (record or {}).get("gates", []):
        out.update(gate.get("blobs") or {})
    return out


HASH_KEYS = ("corpus_sha256", "cases_sha256", "graph_goldens_sha256",
             "nightly_graphs_sha256")


def diff_cohorts(new_tag, old_tag, root=AB):
    """Compare two cohort tags without changing anything.

    Two sources are used on purpose: the tag commit carries RELEASE.json and
    EVIDENCE.json (frozen before the tag), while the gate fingerprints live in
    the record commit, which is a child of the tag.  A record that is missing
    is reported as `None`, not treated as "no difference".
    """
    data = {"new": new_tag, "old": old_tag}
    for label, tag in (("new", new_tag), ("old", old_tag)):
        found = _git_at(root, "rev-parse", "--verify", tag + "^{commit}")
        if found.returncode:
            raise DiffError("unknown tag %r in %s" % (tag, root))
        release = _show_json(root, tag, "RELEASE.json")
        if release is None:
            raise DiffError("%s has no RELEASE.json" % tag)
        data[label] = {
            "tag": tag,
            "commit": found.stdout.strip(),
            "release": release,
            "evidence": _show_json(root, tag, "EVIDENCE.json"),
        }
        data[label]["record_commit"] = _record_for(root, tag)
        data[label]["record"] = (
            _show_json(root, data[label]["record_commit"], "VERIFICATION.json")
            if data[label]["record_commit"] else None)

    new, old = data["new"], data["old"]
    data["commits"] = _delta(old["release"].get("repos"),
                             new["release"].get("repos"))
    data["version"] = {
        "old": old["release"].get("version"),
        "new": new["release"].get("version"),
        "changed": old["release"].get("version") !=
                   new["release"].get("version"),
    }
    data["pin"] = {
        "old": old["release"].get("pin"),
        "new": new["release"].get("pin"),
        "changed": old["release"].get("pin") != new["release"].get("pin"),
    }
    data["hashes"] = {key: _delta(old["release"].get(key),
                                  new["release"].get(key))
                      for key in HASH_KEYS}

    old_gates, new_gates = _gate_index(old["record"]), _gate_index(new["record"])
    rc_changed = []
    command_changed = []
    for name in sorted(set(old_gates) & set(new_gates)):
        if old_gates[name].get("rc") != new_gates[name].get("rc"):
            rc_changed.append({"name": name,
                               "old": old_gates[name].get("rc"),
                               "new": new_gates[name].get("rc")})
        old_command = (old_gates[name].get("manifest_command")
                       or old_gates[name].get("command"))
        new_command = (new_gates[name].get("manifest_command")
                       or new_gates[name].get("command"))
        if old_command != new_command:
            command_changed.append(name)
    data["gates"] = {
        "old": len(old_gates),
        "new": len(new_gates),
        "added": sorted(set(new_gates) - set(old_gates)),
        "removed": sorted(set(old_gates) - set(new_gates)),
        "rc_changed": rc_changed,
        "command_changed": command_changed,
    }
    data["blobs"] = _delta(_blobs(old["record"]), _blobs(new["record"]))
    data["evidence"] = _delta(_evidence_index(old["evidence"]),
                              _evidence_index(new["evidence"]))
    return data


def cmd_diff(args):
    try:
        data = diff_cohorts(args.new, args.old, pathlib.Path(args.root))
    except DiffError as exc:
        print("cohort diff: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    print("cohort diff: %s -> %s" % (args.old, args.new))
    for name, item in sorted(data["commits"]["changed"].items()):
        print("  repo %-8s %s -> %s" % (name, item["old"][:12],
                                        item["new"][:12]))
    for name in data["commits"]["added"]:
        print("  repo %-8s (added)" % name)
    for name in data["commits"]["removed"]:
        print("  repo %-8s (removed)" % name)
    version = data["version"]
    print("  version       %s%s" % (version["new"],
                                    "" if not version["changed"]
                                    else " (was %s)" % version["old"]))
    pin = data["pin"]
    print("  pin           %s" % ("same" if not pin["changed"]
                                    else "%s -> %s" % (pin["old"], pin["new"])))
    for key, label in (("corpus_sha256", "corpus"),
                       ("cases_sha256", "cases"),
                       ("graph_goldens_sha256", "graph"),
                       ("nightly_graphs_sha256", "nightly")):
        item = data["hashes"][key]
        print("  %-13s %d added, %d removed, %d changed"
              % (label, len(item["added"]), len(item["removed"]),
                 len(item["changed"])))
        for name in list(item["changed"])[:5]:
            print("    changed: %s" % name)
    gates = data["gates"]
    print("  gates         %d -> %d, %d rc change(s), %d command change(s)"
          % (gates["old"], gates["new"], len(gates["rc_changed"]),
             len(gates["command_changed"])))
    for item in gates["rc_changed"]:
        print("    rc: %s %s -> %s" % (item["name"], item["old"], item["new"]))
    for name in gates["added"]:
        print("    added gate: %s" % name)
    for name in gates["removed"]:
        print("    removed gate: %s" % name)
    blobs = data["blobs"]
    print("  blobs         %d added, %d removed, %d changed"
          % (len(blobs["added"]), len(blobs["removed"]),
             len(blobs["changed"])))
    for name in list(blobs["changed"])[:8]:
        print("    changed: %s" % name)
    evidence = data["evidence"]
    print("  evidence      %d added, %d removed, %d changed"
          % (len(evidence["added"]), len(evidence["removed"]),
             len(evidence["changed"])))
    for label, side in (("old", data["old"]), ("new", data["new"])):
        if side["record"] is None:
            print("  record        %s: not found (tag-only comparison)" % label)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--online", action="store_true")
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(func=cmd_plan)

    tag = sub.add_parser("tag")
    tag.add_argument("name")
    tag.add_argument("--push", action="store_true")
    tag.set_defaults(func=cmd_tag)

    verify = sub.add_parser("verify")
    verify.add_argument("--clone", required=True)
    verify.add_argument("--out")
    verify.add_argument("--only")
    verify.add_argument("--jobs", type=int, default=4)
    verify.add_argument("--timeout", type=int, default=2400)
    verify.add_argument("--reclone", action="store_true")
    verify.set_defaults(func=cmd_verify)

    ci = sub.add_parser("record-ci")
    ci.add_argument("--file", required=True)
    ci.set_defaults(func=cmd_record_ci)

    check = sub.add_parser("check")
    check.add_argument("--worktree", action="store_true")
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=cmd_check)

    diff = sub.add_parser("diff")
    diff.add_argument("new")
    diff.add_argument("old")
    diff.add_argument("--json", action="store_true")
    diff.add_argument("--root", default=str(AB),
                      help="repository to read (default: this checkout)")
    diff.set_defaults(func=cmd_diff)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
