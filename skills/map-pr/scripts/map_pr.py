#!/usr/bin/env python3
"""Keep a live briefing page for a repo's open pull requests.

Each run is a sync. A pull request is analysed once per head commit, the cheap facts around it (history on the
default branch, other open PRs, checks) are refreshed every run, and a PR that merged or closed moves to the archive.
For each PR the page shows how much there is to read closely, a ledger of assurances and things to watch, hot spots,
a change map and a review order.

Everything is kept under one folder per repo: <home>/<owner>/<repo>/index.html, archive/ and .state/.
Standard library only. Needs gh (signed in), git and Python 3.11+.
"""
import argparse
import ast
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tomllib
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = 1  # bump when the analysis changes, so stored records are redone
ASSETS = Path(__file__).resolve().parent.parent / 'assets'
SETTINGS = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'map-pr' / 'config.toml'
REPO_CONFIG = '.map-pr.toml'
NOT_SET_UP = 3  # exit code when no home folder has been chosen yet

KINDS = {'app': 'Application', 'test': 'Tests', 'config': 'Build & config', 'docs': 'Docs',
         'generated': 'Fixtures & generated'}
GENERATED_DIR = re.compile(r'(^|/)(golden|goldens|snapshots?|__snapshots__|fixtures?|testdata|cassettes)/', re.I)
GENERATED_FILE = re.compile(r'(\.(lock|snap|min\.js|map|g\.dart|freezed\.dart)|_pb2(_grpc)?\.pyi?'
                            r'|^package-lock\.json|^pnpm-lock\.yaml)$')
TEST_DIR = re.compile(r'(^|/)(tests?|features|e2e|__tests__|integration_test)/', re.I)
TEST_FILE = re.compile(r'^(test_.*|.*_test|conftest)\.py$|\.(test|spec)\.[cm]?[jt]sx?$|_test\.(go|dart|rb|rs)$'
                       r'|_(unit)?tests?\.(cc|cpp|c|mm?)$')
TEST_CAMEL = re.compile(r'[a-z0-9]Tests?\.(java|kt|swift|cs)$')  # FooTest.java, matched with its case kept
DOCS = re.compile(r'\.(md|rst|adoc)$')
DOCS_DIR = re.compile(r'(^|/)docs?/', re.I)
CONFIG_FILE = re.compile(r'^(makefile|dockerfile|procfile|requirements.*\.txt|\.gitignore|\.dockerignore)$'
                         r'|\.(toml|ya?ml|cfg|ini|json)$')
CODE = re.compile(r'\.(py|dart|[cm]?[jt]sx?|go|rb|java|kt|swift|rs|c|cc|cpp|h|hpp|cs|php|scala|mm?|sh|sql)$')
GRAPHED = ('.py', '.dart')  # languages whose imports are read
WIDE_CAP = 25  # a file this many others import is widely used, however high the repo's top 10% sits
TRIVIAL = {'else:', 'try:', 'pass', 'return', 'return none', 'continue', 'break', 'finally:', 'raise', '"""', "'''",
           'else {', '} else {', 'return;', '@override'}
PY_DEF = re.compile(r'^\s*(?:async\s+)?(def|class)\s+([A-Za-z]\w*)')
DART_IMPORT = re.compile(r'''^\s*(import|export|part)\s+['"]([^'"]+)['"]''', re.M)

DEFAULTS = {
    'group_roots': ['libs', 'projects', 'packages', 'apps', 'services'],
    'window_days': 90,   # how far back "changes on the default branch" looks
    'max_prs': 30,       # open PRs to keep live, most recently updated first
    'archive_days': 14,  # how long a merged or closed PR stays on the page
    'story_prefix': 'sc-',
    # Paths where a small change has an outsized effect. revert = a git revert doesn't undo everything.
    'areas': {
        'data': {'label': 'Data & schema', 'revert': True,
                 'pattern': r'(^|/)migrations?/|\.sql$|firestore[^/]*\.(rules|indexes)|(^|/)seeds?(/|\.|_)'},
        'deploy': {'label': 'Deploy, CI & scheduled jobs', 'revert': True,
                   'pattern': r'^\.github/|dockerfile|(^|/)makefile$|cloudbuild|\.tf$|^ops/|nginx|procfile'
                              r'|app\.ya?ml$|(^|/)cron/|^\.ci\.ya?ml$'},
        'deps': {'label': 'Dependencies', 'revert': True,
                 'pattern': r'(^|/)(pyproject\.toml|requirements[^/]*\.txt|uv\.lock|poetry\.lock'
                            r'|package(-lock)?\.json|pubspec\.(yaml|lock)|go\.(mod|sum)|Cargo\.(toml|lock)|DEPS)$'},
        'auth': {'label': 'Auth & secrets', 'revert': False,
                 'pattern': r'auth|permission|secur|token|secret|credential|(^|/)keys/'},
        'money': {'label': 'Billing & entitlements', 'revert': False,
                  'pattern': r'entitlement|billing|stripe|payment|subscription|pricing|invoice'},
        'ai': {'label': 'AI instructions', 'revert': False,
               'pattern': r'prompt|\.jinja2?$|(^|/)(CLAUDE|AGENTS|SKILL)\.md$|^\.claude/|^\.gemini/|^\.cursor/'},
        'contract': {'label': 'API contract', 'revert': False, 'pattern': r'openapi|(^|/)schemas?/|\.proto$'},
    },
}
CFG = dict(DEFAULTS)


# ---- shell ----------------------------------------------------------------------------------------------------------

def run(*args: str, cwd: Path | None = None, check: bool = True, stdin: str | None = None) -> str | None:
    """Run a command and return stdout; on failure exit, or return None when check is False."""
    r = subprocess.run(args, cwd=cwd, input=stdin, capture_output=True, text=True)
    if r.returncode:
        if check:
            sys.exit(f'{" ".join(args[:6])} failed: {r.stderr.strip()[-600:]}')
        return None
    return r.stdout


def gh(*args: str, check: bool = True) -> str | None:
    return run('gh', *args, check=check)


# ---- settings -------------------------------------------------------------------------------------------------------

def resolve_home(flag: str | None) -> Path:
    """Where briefings live: --home, then MAP_PR_HOME, then the settings file. Exits if none is set."""
    home = flag or os.environ.get('MAP_PR_HOME')
    if not home and SETTINGS.is_file():
        home = tomllib.loads(SETTINGS.read_text()).get('home')
    if not home:
        print('map-pr is not set up yet. Ask the user which folder the PR briefings should be kept in '
              '(the default is ~/map-pr), then run this script with --set-home <folder>.')
        sys.exit(NOT_SET_UP)
    return Path(home).expanduser()


def set_home(folder: str) -> None:
    path = Path(folder).expanduser().absolute()
    path.mkdir(parents=True, exist_ok=True)
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(f'# Where map-pr keeps its PR briefings. Change it by rerunning map_pr.py --set-home.\n'
                        f'home = {json.dumps(str(path))}\n')
    print(f'Briefings will be kept in {path} (saved in {SETTINGS})')


def load_repo_config(repo: Path, branch: str) -> None:
    """Lay the repo's optional .map-pr.toml (read from its default branch) over the defaults."""
    text = run('git', 'show', f'origin/{branch}:{REPO_CONFIG}', cwd=repo, check=False)
    over = tomllib.loads(text) if text else {}
    CFG.update({k: over[k] for k in DEFAULTS if k in over and k != 'areas'})
    areas = {k: dict(v) for k, v in DEFAULTS['areas'].items()}
    for key, area in (over.get('areas') or {}).items():
        areas[key] = {**areas.get(key, {'label': key, 'revert': False}), **area}
    CFG['areas'] = {k: v for k, v in areas.items() if v.get('pattern')}  # an empty pattern turns an area off
    for area in CFG['areas'].values():
        area['re'] = re.compile(area['pattern'], re.I)


def config_hash() -> str:
    plain = {k: v for k, v in CFG.items() if k != 'areas'}
    plain['areas'] = {k: [a['label'], a['pattern'], a['revert']] for k, a in CFG['areas'].items()}
    return hashlib.sha1(json.dumps([SCHEMA, plain], sort_keys=True).encode()).hexdigest()[:12]


# ---- paths ----------------------------------------------------------------------------------------------------------

def kind_of(path: str) -> str:
    """Classify a path as app, test, config, docs or generated."""
    name = path.rsplit('/', 1)[-1].lower()
    if GENERATED_DIR.search(path) or GENERATED_FILE.search(name):
        return 'generated'
    if TEST_DIR.search(path) or TEST_FILE.search(name) or TEST_CAMEL.search(path):
        return 'test'
    if DOCS.search(name) or DOCS_DIR.search(path):
        return 'docs'
    if CONFIG_FILE.search(name) or path.startswith('.github/'):
        return 'config'
    return 'app'


def module_of(path: str) -> str:
    """Return the library or project a path belongs to, e.g. packages/core."""
    parts = path.split('/')
    if parts[0] in CFG['group_roots'] and len(parts) > 2:
        return '/'.join(parts[:2])
    return parts[0] if len(parts) > 1 else '(root)'


def is_code(path: str) -> bool:
    return bool(CODE.search(path))


# ---- the skill's own clone ------------------------------------------------------------------------------------------

def ensure_clone(repo: Path, url: str) -> None:
    """Create the bare clone the analysis reads from. It never touches the user's own checkouts."""
    if (repo / 'HEAD').exists():
        return
    repo.mkdir(parents=True, exist_ok=True)
    run('git', 'init', '-q', '--bare', cwd=repo)
    run('git', 'remote', 'add', 'origin', url + '.git', cwd=repo)
    host = url.split('/')[2]
    run('git', 'config', f'credential.https://{host}.helper', '!gh auth git-credential', cwd=repo)


def fetch_branch(repo: Path, branch: str) -> None:
    """Fetch the default branch's recent history: commits and trees only, back past the touch-rate window."""
    since = (datetime.now(timezone.utc) - timedelta(days=CFG['window_days'] + 14)).strftime('%Y-%m-%d')
    spec = f'+refs/heads/{branch}:refs/remotes/origin/{branch}'
    base = ('git', 'fetch', '-q', '--no-tags', '--filter=blob:none')
    if run(*base, f'--shallow-since={since}', 'origin', spec, cwd=repo, check=False) is None:
        run(*base, '--depth=50', 'origin', spec, cwd=repo)  # no commits in the window


def fetch_heads(repo: Path, numbers: list[int]) -> None:
    for i in range(0, len(numbers), 40):
        specs = [f'+refs/pull/{n}/head:refs/remotes/pr/{n}' for n in numbers[i:i + 40]]
        run('git', 'fetch', '-q', '--no-tags', '--filter=blob:none', '--depth=1', 'origin', *specs, cwd=repo)


def source_blobs(repo: Path, ref: str) -> list[tuple[str, str]]:
    """(blob id, path) for every file at ref whose imports can be read."""
    out = []
    for line in run('git', 'ls-tree', '-r', ref, cwd=repo).splitlines():
        meta, path = line.split('\t', 1)
        if path.endswith(GRAPHED) and meta.split()[1] == 'blob':
            out.append((meta.split()[2], path))
    return out


# ---- imports --------------------------------------------------------------------------------------------------------

def parse_imports(path: str, text: str) -> list:
    """The raw imports of one file, in a form that can be cached by blob id."""
    if path.endswith('.dart'):
        return [[m[1] != 'import', m[2]] for m in DART_IMPORT.finditer(text) if not m[2].startswith('dart:')]
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [[0, a.name, None] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append([node.level, node.module or '', [a.name for a in node.names]])
    return out


class Parsed:
    """Parsed imports by blob id, kept on disk so a file is fetched and parsed once however many PRs carry it."""

    def __init__(self, repo: Path, cache: Path):
        self.repo, self.cache = repo, cache
        self.by_blob = json.loads(cache.read_text()) if cache.is_file() else {}

    def load(self, blobs: list[tuple[str, str]]) -> None:
        todo = {oid: path for oid, path in blobs if oid not in self.by_blob}
        if not todo:
            return
        have = set(run('git', 'cat-file', '--batch-check=%(objectname)', '--batch-all-objects', '--unordered',
                       cwd=self.repo).split())
        missing = [oid for oid in todo if oid not in have]
        for i in range(0, len(missing), 5000):  # one request per batch, not one per file
            run('git', '-c', 'fetch.negotiationAlgorithm=noop', 'fetch', '-q', '--no-tags', '--no-write-fetch-head',
                '--recurse-submodules=no', '--filter=blob:none', '--stdin', 'origin', cwd=self.repo,
                stdin='\n'.join(missing[i:i + 5000]) + '\n')
        r = subprocess.run(['git', 'cat-file', '--batch'], cwd=self.repo, input='\n'.join(todo).encode() + b'\n',
                           capture_output=True, check=True)
        data, pos = r.stdout, 0
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')  # SyntaxWarning from the code being read
            while pos < len(data):
                end = data.index(b'\n', pos)
                oid, kind, size = data[pos:end].decode().split()
                body = data[end + 1:end + 1 + int(size)]
                pos = end + 2 + int(size)
                self.by_blob[oid] = parse_imports(todo[oid], body.decode('utf-8', 'replace'))
        self.cache.write_text(json.dumps(self.by_blob))


class Graph:
    """Who imports what at one commit. An import of a file that re-exports another counts as importing both."""

    def __init__(self, blobs: list[tuple[str, str]], parsed: Parsed):
        self.paths = {path for _, path in blobs}
        self.rev, self.exported_by = defaultdict(set), defaultdict(set)
        self._py, self._dart = defaultdict(list), defaultdict(list)
        for path in self.paths:
            parts = path.rsplit('.', 1)[0].split('/')
            if path.endswith('.py'):
                if parts[-1] == '__init__':
                    parts = parts[:-1]
                for i in range(len(parts)):
                    self._py['.'.join(parts[i:])].append(path)
            else:
                segs = path.split('/')
                for i in range(1, len(segs) - 1):
                    if segs[i] == 'lib':
                        self._dart[(segs[i - 1], '/'.join(segs[i + 1:]))].append(path)
        for oid, path in blobs:
            for target, export in self._targets(path, parsed.by_blob.get(oid, [])):
                if target != path:
                    self.rev[target].add(path)
                    if export:
                        self.exported_by[target].add(path)

    def _pick(self, cands: list[str], importer: str) -> str | None:
        same = [c for c in cands if module_of(c) == module_of(importer)]
        return min(same or cands, key=len) if cands else None

    def _python(self, name: str, importer: str) -> str | None:
        cands = self._py.get(name)
        if not cands:
            return None
        here = importer.rsplit('/', 1)[0]
        near = [c for c in cands if c.rsplit('/', 1)[0] == here or c.rsplit('/', 2)[0] == here]
        if near:
            return near[0]
        if '.' not in name:  # a bare name is only trusted when it's a package
            cands = [c for c in cands if c.endswith('/__init__.py')]
        return self._pick(cands, importer)

    def _targets(self, path: str, imports: list):
        """Yield (imported path, is it a re-export) for one file."""
        here = path.rsplit('/', 1)[0] if '/' in path else ''
        if path.endswith('.dart'):
            for export, uri in imports:
                if uri.startswith('package:'):
                    pkg, _, rest = uri[8:].partition('/')
                    target = self._pick(self._dart.get((pkg, rest), []), path)
                else:
                    target = os.path.normpath(os.path.join(here, uri))
                if target in self.paths:
                    yield target, export
            return
        init = path.endswith('/__init__.py')
        pkg = here.split('/') if here else []
        for level, module, names in imports:
            found = []
            if level:
                stem = '/'.join(pkg[:len(pkg) - level + 1] + (module.split('.') if module else []))
                for name in names:
                    for cand in (f'{stem}/{name}.py', f'{stem}/{name}/__init__.py', f'{stem}.py',
                                 f'{stem}/__init__.py'):
                        if cand in self.paths:
                            found.append(cand)
                            break
            elif names is None:  # import a.b.c
                parts = module.split('.')
                for i in range(len(parts), 0, -1):
                    if t := self._python('.'.join(parts[:i]), path):
                        found.append(t)
                        break
            else:  # from module import names: prefer the submodule, fall back to the package
                found = [t for name in names if (t := self._python(f'{module}.{name}', path))]
                if not found and (t := self._python(module, path)):
                    found.append(t)
            for target in found:
                # A package's __init__ importing from inside the package is a re-export
                yield target, init and target.startswith(here + '/')

    def dependents(self, path: str) -> set[str]:
        """Files that import path, directly or through a file that re-exports it."""
        out, todo, seen = set(), [path], {path}
        while todo:
            x = todo.pop()
            out |= self.rev.get(x, set())
            for via in self.exported_by.get(x, ()):
                if via not in seen:
                    seen.add(via)
                    todo.append(via)
        out.discard(path)
        return out

    def reach(self, path: str) -> set[str]:
        seen, todo = set(), [path]
        while todo:
            for nxt in self.rev.get(todo.pop(), ()):
                if nxt not in seen and nxt != path:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen


# ---- moved lines and signatures -------------------------------------------------------------------------------------

def norm(line: str) -> str:
    return re.sub(r'\s+', ' ', line.strip())


def significant(n: str) -> bool:
    return len(n) >= 4 and sum(c.isalnum() for c in n) >= 3 and n.lower() not in TRIVIAL


def patch_runs(patch: str) -> tuple[list[list[str]], list[list[str]]]:
    """Split a patch into runs of consecutive removed lines and runs of consecutive added lines."""
    rem, add, cur, sign = [], [], [], ''
    for line in (patch or '').splitlines() + ['@@']:
        s = line[:1] if line[:1] in ('+', '-') else ''
        if s != sign:
            if cur:
                (add if sign == '+' else rem).append(cur)
            cur, sign = [], s
        if s:
            cur.append(norm(line[1:]))
    return rem, add


def moved_lines(runs: list[list[str]], pool: Counter) -> tuple[int, list[str]]:
    """Count lines in runs that also appear in pool, keeping only stretches of 3 or more matching lines."""
    total, kept = 0, []
    for run_ in runs:
        stretch, hits = [], 0
        for n in run_ + [None]:
            if n is not None and (not significant(n) or pool.get(n)):
                stretch.append(n)
                hits += significant(n)
                continue
            if hits >= 3:
                while stretch and not significant(stretch[-1]):
                    stretch.pop()
                total += len(stretch)
                kept += [x for x in stretch if significant(x)]
            stretch, hits = [], 0
    return total, kept


def detect_moves(files: list[dict], patches: dict[str, str]) -> list[dict]:
    """Set moved_in and moved_out on each file and return the largest file-to-file flows."""
    runs = {f['path']: patch_runs(patches.get(f['path'], '')) for f in files}
    removed_pool, added_pool, added_where = Counter(), Counter(), defaultdict(Counter)
    for path, (rem, add) in runs.items():
        for r in rem:
            removed_pool.update(n for n in r if significant(n))
        for r in add:
            for n in r:
                if significant(n):
                    added_pool[n] += 1
                    added_where[n][path] += 1
    flows = Counter()
    for f in files:
        rem, add = runs[f['path']]
        out, kept = moved_lines(rem, added_pool)
        f['moved_out'], f['moved_in'] = min(out, f['d']), min(moved_lines(add, removed_pool)[0], f['a'])
        for n in kept:
            others = {p: c for p, c in added_where[n].items() if p != f['path']}
            flows[(f['path'], max(others, key=others.get) if others else f['path'])] += 1
    return [{'from': a, 'to': b, 'lines': n} for (a, b), n in flows.most_common(6) if n >= 3]


def signatures(files: list[dict], patches: dict[str, str]) -> None:
    """Public Python defs and classes whose definition line was rewritten, removed or moved to another file."""
    removed, added, defined = {}, {}, defaultdict(set)
    for f in files:
        rem, add = {}, {}
        for line in patches.get(f['path'], '').splitlines() if f['path'].endswith('.py') else ():
            if line[:1] in ('+', '-') and (m := PY_DEF.match(line[1:])):
                if line[0] == '+':
                    defined[m[2]].add(f['path'])
                if not m[2].startswith('_'):
                    (add if line[0] == '+' else rem)[m[2]] = norm(line[1:])
        removed[f['path']], added[f['path']] = rem, add
    for f in files:
        rem, add = removed[f['path']], added[f['path']]
        gone = [n for n in rem if n not in add]
        f['sig'] = {'changed': sorted(n for n in rem if n in add and add[n] != rem[n]),
                    'moved': sorted(n for n in gone if defined[n] - {f['path']}),
                    'removed': sorted(n for n in gone if not defined[n] - {f['path']})}


# ---- analysis of one PR, kept until its head commit changes -----------------------------------------------------------

def test_stem(name: str) -> str:
    stem = re.sub(r'\.(test|spec)$', '', name.rsplit('.', 1)[0])
    return re.sub(r'^test_|_(unit)?tests?$|Tests?$', '', stem)


def analyse(slug: str, pr: dict, graph: Graph) -> dict:
    """Everything about a PR that depends only on its own commits: files, moves, pairing and dependents."""
    n = pr['number']
    rows = [json.loads(line) for line in
            gh('api', f'repos/{slug}/pulls/{n}/files?per_page=100', '--paginate', '--jq', '.[] | tojson').splitlines()]
    patches = {r['filename']: r.get('patch') or '' for r in rows}
    changed = set(patches)
    files = []
    for r in rows:
        path = r['filename']
        mod, kind = module_of(path), kind_of(path)
        rest = path if mod == '(root)' else path[len(mod) + 1:]
        deps = graph.dependents(path) if path.endswith(GRAPHED) else set()
        users = sorted(p for p in deps if kind_of(p) != 'test')
        outside = [p for p in users if p not in changed]
        f = {'path': path, 'a': r['additions'], 'd': r['deletions'], 'status': r['status'],
             'prev': r.get('previous_filename', ''), 'kind': kind, 'mod': mod, 'dir': rest.rpartition('/')[0],
             'name': path.rsplit('/', 1)[-1], 'code': is_code(path),
             'url': f'{pr["url"]}/files#diff-{hashlib.sha256(path.encode()).hexdigest()}',
             'fanin': len(users), 'fanin_out': len(outside), 'fanin_tests': len(deps) - len(users),
             'fanin_mods': len({module_of(p) for p in outside}),
             'reach': len([p for p in graph.reach(path) if kind_of(p) != 'test']) if deps else 0,
             'dependents': outside[:14],
             'deps_in_pr': [], 'tests': [], 'tested_by': [], 'existing_tests': 0, 'flags': []}
        if kind != 'test':
            f['flags'] = [key for key, area in CFG['areas'].items() if area['re'].search(path)
                          and (kind != 'docs' or key == 'ai') and (kind != 'generated' or key == 'contract')]
        f['_deps'] = deps
        files.append(f)
    flows = detect_moves(files, patches)
    signatures(files, patches)

    # Pair each changed test with the changed source file it tests: by name, then by import
    app = [f for f in files if f['kind'] == 'app' and is_code(f['path'])]
    for f in app:
        f['deps_in_pr'] = sorted(g['path'] for g in app if g is not f and f['path'] in g['_deps'])
        f['existing_tests'] = len([p for p in f['_deps'] if kind_of(p) == 'test' and p not in changed])
    for t in (f for f in files if f['kind'] == 'test' and is_code(f['path'])):
        stem = test_stem(t['name'])
        imported = [f for f in app if t['path'] in f['_deps']]
        named = [f for f in app if f['name'].rsplit('.', 1)[0] == stem] or \
            sorted((f for f in app if stem.startswith(f['name'].rsplit('.', 1)[0] + '_') and f['mod'] == t['mod']),
                   key=lambda f: -len(f['name']))
        primary = (named or sorted(imported, key=lambda f: -(f['a'] + f['d'])) or [None])[0]
        t['covers'] = primary['path'] if primary else ''
        if primary:
            primary['tests'].append(t['path'])
        for f in {id(x): x for x in imported + ([primary] if primary else [])}.values():
            f['tested_by'].append(t['path'])
    for f in files:
        del f['_deps']
    return {'files': files, 'flows': flows}


# ---- facts that change without the PR changing: recomputed every sync ------------------------------------------------

def history(repo: Path, branch: str) -> tuple[Counter, dict, int]:
    """How many changes on the default branch touched each file in the window, and when the last one was."""
    shallow = set((repo / 'shallow').read_text().split()) if (repo / 'shallow').is_file() else set()
    log = run('git', 'log', f'origin/{branch}', '--first-parent', '--diff-merges=first-parent',
              f'--since={CFG["window_days"]} days ago', '--name-only', '--format=@%H %cs', cwd=repo)
    touches, last, commits, skip = Counter(), {}, 0, False
    for line in log.splitlines():
        if line.startswith('@'):
            sha, day = line[1:].split()
            skip = sha in shallow  # a cut-off commit lists every file as changed
            commits += not skip
        elif line and not skip:
            touches[line] += 1
            last.setdefault(line, day)
    return touches, last, commits


def thresholds(graph: Graph, touches: Counter, commits: int) -> dict:
    """The top-10% marks for touch rate and dependents, measured across the whole repo."""
    tests = {p for p in graph.paths if kind_of(p) == 'test'}
    counted = [p for p in graph.paths if p not in tests and kind_of(p) == 'app']
    fanin = sorted(n for n in (len(graph.dependents(p) - tests) for p in counted) if n)
    touched = sorted(c for p, c in touches.items() if kind_of(p) == 'app' and is_code(p))
    top = lambda xs: xs[min(len(xs) - 1, int(len(xs) * .9))] if xs else 0  # noqa: E731
    return {'touches': max(3, top(touched)), 'fanin': min(WIDE_CAP, max(3, top(fanin))),
            'fanin_top': top(fanin) <= WIDE_CAP, 'window_days': CFG['window_days'],
            'commits': commits, 'files_touched': len(touched), 'files_imported': len(fanin),
            'graphed': bool(graph.paths)}


def checks_of(pr: dict) -> dict:
    tally = Counter()
    for c in pr.get('statusCheckRollup') or []:
        state = (c.get('conclusion') or c.get('state') or c.get('status') or '').upper()
        tally['pass' if state in ('SUCCESS', 'NEUTRAL', 'SKIPPED') else
              'fail' if state in ('FAILURE', 'ERROR', 'TIMED_OUT', 'CANCELLED', 'ACTION_REQUIRED', 'STARTUP_FAILURE')
              else 'pending'] += 1
    return dict(tally)


def plural(n: int, word: str, words: str | None = None) -> str:
    return f'{n:,} {word if n == 1 else (words or word + "s")}'


def listed(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else ', '.join(xs[:-1]) + ' and ' + xs[-1]


def low(label: str) -> str:
    return label if re.match(r'[A-Z]{2}', label) else label[0].lower() + label[1:]


def churn(f: dict) -> int:
    return f['a'] + f['d']


def moved(f: dict) -> int:
    return min(churn(f), f['moved_in'] + f['moved_out'])


def net(f: dict) -> int:
    return churn(f) - moved(f)


def ledger(v: dict, T: dict) -> list[dict]:
    """Ask the same review questions of every PR. Each comes back as an assurance or as something to watch."""
    F = v['files']
    app = [f for f in F if f['kind'] == 'app']
    src = [f for f in app if is_code(f['path'])]
    graphed = [f for f in src if f['path'].endswith(GRAPHED)]
    out = []

    def add(key: str, state: str, title: str, evidence: str, files: list[dict] = (), note=None) -> None:
        out.append({'id': key, 'state': state, 't': title, 'ev': evidence, 'more': max(0, len(files) - 8),
                    'files': [{'path': f['path'], 'note': note(f) if note else ''} for f in files[:8]]})

    c, base = v['checks'], v['base']
    ran = sum(c.values())
    if v['state'] == 'OPEN':
        if c.get('fail'):
            add('ci', 'watch', f'{plural(c["fail"], "check")} failing',
                f'{c.get("pass", 0)} passing, {c["fail"]} failing at commit {v["head"]}.')
        elif c.get('pending'):
            add('ci', 'watch', 'Checks still running', f'{c["pending"]} pending at commit {v["head"]}.')
        elif ran:
            add('ci', 'held', 'Checks pass', f'{plural(c["pass"], "check")} green at commit {v["head"]}.')
        else:
            why = ', probably because this PR targets another PR\'s branch' if v['stacked'] else ''
            add('ci', 'watch', 'No checks have run', f'GitHub reports no check runs for commit {v["head"]}{why}. '
                'Nothing has verified that the tests pass.')
        if v['mergeable'] == 'CONFLICTING':
            add('merge', 'watch', 'Conflicts with its base branch',
                f'It can\'t merge into `{base}` as is. Resolving the conflicts will change this diff.')
        elif v['mergeable'] == 'MERGEABLE':
            add('merge', 'held', 'Merges cleanly', f'No conflicts with `{base}`.')

    areas = CFG['areas']
    for key, revert in (('sensitive', False), ('revert', True)):
        keys = [k for k, a in areas.items() if a['revert'] == revert]
        hit = [f for f in F if set(f['flags']) & set(keys)]
        names = [low(areas[k]['label']) for k in keys if any(k in f['flags'] for f in hit)]
        note = (lambda f, keys=keys: ', '.join(low(areas[k]['label']) for k in f['flags'] if k in keys)) \
            if len(names) > 1 else None
        if hit and revert:
            add(key, 'watch', 'A revert may not undo everything', f'Changes {listed(names)}. Reverting the commit '
                'won\'t undo data already written or jobs already run.', hit, note)
        elif hit:
            add(key, 'watch', f'Touches {listed(names)}', f'{plural(len(hit), "file")} matched by path. These are '
                'areas where a small change has an outsized effect.', hit, note)
        elif revert:
            add(key, 'held', 'Easily reversed', 'No data, dependency or deploy changes, so a revert restores the '
                'previous behaviour.')
        else:
            add(key, 'held', 'No sensitive areas touched', 'No ' + listed(names_of(keys)) + ' paths.')

    wide = sorted((f for f in app if f['fanin_out'] >= T['fanin']), key=lambda f: -f['fanin_out'])
    hot = sorted((f for f in app if f['touches'] >= T['touches']), key=lambda f: -f['touches'])
    if wide:
        add('deps', 'watch', f'Changes {plural(len(wide), "widely used file")}',
            f'Each is imported by {T["fanin"]} or more files outside this PR'
            + (', which puts it in the top 10% of the repo' if T['fanin_top'] else '')
            + '. None of those files are in this diff.', wide,
            lambda f: f'{f["fanin_out"]:,} dependents in {plural(f["fanin_mods"], "module")}')
    elif graphed:
        most = max(f['fanin_out'] for f in graphed)
        add('deps', 'held', 'No widely used files changed', f'The most-imported changed file has '
            f'{plural(most, "dependent")} outside this PR. The bar for "widely used" is {T["fanin"]}.')
    if hot:
        add('hot', 'watch', f'Touches {plural(len(hot), "hot spot")}',
            f'Changed {T["touches"]} or more times on `{v["default"]}` in the last {T["window_days"]} days (top 10% '
            'of the repo). Files that change this often are where regressions and merge conflicts collect.',
            hot, lambda f: plural(f['touches'], 'change'))
    elif app:
        add('hot', 'held', 'Stays out of hot spots', f'No changed source file was changed {T["touches"]} or more '
            f'times on `{v["default"]}` in the last {T["window_days"]} days.')

    py = [f for f in src if f['path'].endswith('.py')]
    sig = [f for f in py if any(f['sig'].values())]
    sig_out = [f for f in sig if f['fanin_out']]
    if sig_out:
        add('contract', 'watch', f'Public names changed in {plural(len(sig_out), "file")} that other code imports',
            'A def or class line was rewritten, removed or moved, and files outside this PR import the module. '
            'Check the callers.', sig_out,
            lambda f: ', '.join([f'{n} changed' for n in f['sig']['changed']] +
                                [f'{n} removed' for n in f['sig']['removed']] +
                                [f'{n} moved' for n in f['sig']['moved']]))
    elif sig:
        add('contract', 'held', 'Signature changes stay inside this PR',
            f'Only files in this PR import the {plural(len(sig), "module")} whose def or class lines changed.')
    elif py:
        add('contract', 'held', 'No public signatures changed',
            'No public def or class line was rewritten or removed. (Single-line signatures only.)')

    need = [f for f in src if net(f) >= 5]
    untested = [f for f in need if not f['tested_by']]
    close, tests = sum(net(f) for f in app), sum(net(f) for f in F if f['kind'] == 'test')
    ratio = f'Tests change {tests / close:.1f} lines per source line.' if close else ''
    if need and not untested:
        add('tests', 'held', 'Every changed source file has a changed test',
            f'All {plural(len(need), "source file")} with 5 or more changed lines '
            f'{"is" if len(need) == 1 else "are"} covered by a test this PR changes. {ratio}')
    elif untested:
        add('tests', 'watch', f'{len(untested)} of {plural(len(need), "source file")} '
            f'{"has" if len(untested) == 1 else "have"} no changed test',
            f'No test changed in this PR is named after or imports {"it" if len(untested) == 1 else "them"}. '
            f'{ratio}', untested,
            lambda f: f'{plural(f["existing_tests"], "existing test")} unchanged' if f['existing_tests']
            else 'no changed test')

    mods = v['mods']
    if len(mods) == 1:
        add('scope', 'held', 'Isolated to one module', f'All application changes are in `{mods[0]}`.')
    elif mods:
        sizes = [f'`{m.split("/")[-1]}` ({sum(net(f) for f in app if f["mod"] == m):,})' for m in mods[:8]]
        add('scope', 'watch', f'Spans {len(mods)} modules', listed(sizes) + ('' if len(mods) <= 8 else ' and more')
            + '. Lines of application code in each.')
    else:
        add('scope', 'held', 'No application code changed', 'Only docs, config, tests or fixtures.')
    src_net = sum(net(f) for f in src)
    fresh = sum(net(f) for f in src if not f['on_main'] or f['status'] == 'added')
    if src_net >= 20 and fresh / src_net >= .8:
        add('additive', 'held', 'Mostly new code', f'{round(fresh / src_net * 100)}% of source lines are in files '
            f'that aren\'t on `{v["default"]}` yet, so code already there changes by {plural(src_net - fresh, "line")}.')

    if v['state'] == 'OPEN':
        shared = sorted((f for f in F if f['other_prs']), key=lambda f: -len(f['other_prs']))
        if shared:
            prs = sorted({o['n'] for f in shared for o in f['other_prs']})
            same = '' if any(not o['stack'] for f in shared for o in f['other_prs']) else ' (all in the same stack)'
            add('overlap', 'watch', f'{plural(len(shared), "file")} also changed by other open PRs',
                ', '.join(f'#{n}' for n in prs[:12]) + ('…' if len(prs) > 12 else '') + same +
                '. Whichever merges first changes what the others are diffed against.', shared,
                lambda f: plural(len(f['other_prs']), 'other PR'))
        else:
            add('overlap', 'held', 'No other open PR touches these files',
                f'Checked against the {plural(v["open_count"], "open PR")} on this page.')
        below, above = v['stack_below'], v['stack_above']
        if below:
            add('stack', 'watch', f'Stacked on {plural(len(below), "unmerged PR")}', 'This diff shows only the top '
                f'of the stack. {", ".join("#" + str(s["n"]) for s in below)} must merge first.')
        if above:
            add('stack2', 'watch', f'{plural(len(above), "open PR")} build{"s" if len(above) == 1 else ""} on this '
                'one', f'{", ".join("#" + str(s["n"]) for s in above)}. A change requested here ripples up through '
                f'{"it" if len(above) == 1 else "all of them"}.')
    return out


def names_of(keys: list[str]) -> list[str]:
    return [low(CFG['areas'][k]['label']) for k in keys] or ['sensitive']


def view_of(rec: dict, pr: dict, facts: dict) -> dict:
    """The record as the page shows it: the stored analysis plus today's facts, magnitude, ledger and layers."""
    T, touches, last, on_main = facts['T'], facts['touches'], facts['last'], facts['on_main']
    n = pr['number']
    files = [dict(f) for f in rec['files']]
    for f in files:
        f['touches'], f['last'], f['on_main'] = touches.get(f['path'], 0), last.get(f['path'], ''), f['path'] in on_main
        f['other_prs'] = [{'n': m, 'stack': m in facts['related'].get(n, ())}
                          for m, paths in facts['pr_files'].items() if m != n and f['path'] in paths]
    app = [f for f in files if f['kind'] == 'app']
    depth = {}

    def layer(path: str, seen: frozenset = frozenset()) -> int:
        if path not in depth:
            if path in seen:
                return 0
            deps = [d for d in by_path[path]['deps_in_pr'] if d in by_path]
            depth[path] = 1 + max(layer(d, seen | {path}) for d in deps) if deps else 0
        return depth[path]

    by_path = {f['path']: f for f in app}
    for f in app:
        f['layer'] = layer(f['path'])
        f['risk'] = round(f['touches'] / T['touches'] + f['fanin_out'] / T['fanin'] + len(f['flags']) * .5
                          + (len(f['sig']['changed']) + len(f['sig']['removed'])) * .3 + min(1, net(f) / 400), 3)
    by_kind = {k: [f for f in files if f['kind'] == k] for k in KINDS}
    mods = [m for m, _ in Counter({m: sum(net(f) for f in app if f['mod'] == m) for m in {f['mod'] for f in app}})
            .most_common()]
    stack = facts['stacks'].get(n, {'below': [], 'above': [], 'root': pr['baseRefName']})
    v = {'number': n, 'title': pr['title'], 'url': pr['url'], 'state': pr.get('state', 'OPEN'),
         'draft': pr.get('isDraft', False), 'head': pr['headRefOid'][:7], 'base': pr['baseRefName'],
         'default': facts['branch'], 'author': (pr.get('author') or {}).get('login', ''),
         'created': pr.get('createdAt', '')[:10], 'updated': pr.get('updatedAt', '')[:10],
         'checks': checks_of(pr), 'review': pr.get('reviewDecision') or '', 'mergeable': pr.get('mergeable') or '',
         'stack_below': stack['below'], 'stack_above': stack['above'], 'root_base': stack['root'],
         'stacked': pr['baseRefName'] != facts['branch'], 'open_count': len(facts['pr_files']),
         'flows': rec['flows'], 'files': files, 'mods': mods,
         'mag': {'total': sum(churn(f) for f in files), 'close': sum(net(f) for f in app),
                 'moved': sum(moved(f) for f in files if f['kind'] != 'generated'),
                 'test': sum(net(f) for f in by_kind['test']), 'config': sum(net(f) for f in by_kind['config']),
                 'docs': sum(net(f) for f in by_kind['docs']), 'generated': sum(churn(f) for f in by_kind['generated'])}}
    v['ledger'] = ledger(v, T)
    return v


# ---- targets --------------------------------------------------------------------------------------------------------

def story_pr(story: str, slug: str) -> tuple[int, str]:
    """Find the PR whose title or branch names the story, preferring open then most recently updated."""
    tag = CFG['story_prefix'] + story
    prs = json.loads(gh('pr', 'list', '--repo', slug, '--state', 'all', '--search', tag, '--limit', '30',
                        '--json', 'number,title,headRefName,state,updatedAt'))
    pat = re.compile(rf'(?<![\w]){re.escape(tag)}(?!\d)', re.I)
    prs = [p for p in prs if pat.search(p['title']) or pat.search(p['headRefName'])]
    if not prs:
        sys.exit(f'No PR in {slug} has {tag} in its title or branch name')
    prs.sort(key=lambda p: (p['state'] == 'OPEN', p['updatedAt']), reverse=True)
    note = f'Story {tag} -> PR #{prs[0]["number"]} ({prs[0]["state"].lower()})'
    if len(prs) > 1:
        note += '; other PRs for this story: ' + ', '.join(f'#{p["number"]} ({p["state"].lower()})' for p in prs[1:])
    return prs[0]['number'], note


def resolve(target: str, slug: str) -> tuple[int, str]:
    """Turn a PR number or story id into a PR number and a note on how it was found."""
    t = target.strip().lower().lstrip('#')
    prefix = re.escape(CFG['story_prefix'].rstrip('-')) if CFG['story_prefix'] else None
    if prefix and (m := re.fullmatch(rf'(?:{prefix}-?|story\s*)(\d+)', t)):
        return story_pr(m[1], slug)
    if not (m := re.fullmatch(r'(?:pr\s*)?(\d+)', t)):
        sys.exit(f'Not a PR number or story id: {target}')
    # gh echoes a requested number without a lookup, so ask for a field only the server knows
    if gh('pr', 'view', m[1], '--repo', slug, '--json', 'url', check=False):
        return int(m[1]), ''
    if not prefix:
        sys.exit(f'No PR #{m[1]} in {slug}')
    n, note = story_pr(m[1], slug)
    return n, f'No PR #{m[1]} in {slug}, so treated it as a story. {note}'


# ---- sync -----------------------------------------------------------------------------------------------------------

PR_FIELDS = ('number,title,url,state,isDraft,isCrossRepository,headRefOid,headRefName,baseRefName,author,createdAt,'
             'updatedAt,mergedAt,closedAt,statusCheckRollup,reviewDecision,mergeable')


def stacks_of(prs: list[dict]) -> tuple[dict, dict]:
    """For each PR, the open PRs it is stacked on and the ones stacked on it."""
    by_head = {p['headRefName']: p for p in prs if not p.get('isCrossRepository')}
    below = {}
    for p in prs:
        chain, cur = [], p
        while (parent := by_head.get(cur['baseRefName'])) and parent is not cur and parent['number'] not in chain \
                and parent is not p:
            chain.append(parent['number'])
            cur = parent
        below[p['number']] = (chain, cur['baseRefName'])
    title = {p['number']: p['title'] for p in prs}
    stacks, related = {}, {}
    for p in prs:
        n = p['number']
        chain, root = below[n]
        above = sorted(m for m, (c, _) in below.items() if n in c)
        stacks[n] = {'below': [{'n': m, 'title': title[m]} for m in reversed(chain)],
                     'above': [{'n': m, 'title': title[m]} for m in above], 'root': root}
        related[n] = set(chain) | set(above)
    return stacks, related


def write_page(path: Path, data: dict, title: str, body_only: bool = False) -> None:
    page = (ASSETS / 'template.html').read_text()
    page = page.replace('__TITLE__', html.escape(title))
    page = page.replace('__TREEMAP_LIB__', (ASSETS / 'd3-hierarchy.min.js').read_text())
    page = page.replace('__DATA__', json.dumps(data, separators=(',', ':')).replace('</', '<\\/'))
    if not body_only:
        page = f'<!doctype html>\n<html lang="en">\n{page}</html>\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('target', nargs='?', help='PR number (1100, #1100) or story id (sc-5204) to point at after the '
                    'sync; default: the PR for the current branch, if it has one')
    ap.add_argument('--repo', help='owner/name; default: the current directory\'s GitHub repo')
    ap.add_argument('--home', help='folder to keep briefings in, for this run only')
    ap.add_argument('--set-home', metavar='FOLDER', help='save the folder to keep briefings in, then exit')
    ap.add_argument('--max-prs', type=int, help=f'open PRs to keep live (default {DEFAULTS["max_prs"]})')
    ap.add_argument('--force', action='store_true', help='analyse every PR again, even if its commits are unchanged')
    ap.add_argument('--artifact', action='store_true',
                    help='also write the page as a page body for the Claude Artifact tool')
    args = ap.parse_args()
    if args.set_home:
        return set_home(args.set_home)
    home = resolve_home(args.home)

    info = json.loads(gh('repo', 'view', *([args.repo] if args.repo else []), '--json',
                         'nameWithOwner,url,defaultBranchRef'))
    slug, branch = info['nameWithOwner'], info['defaultBranchRef']['name']
    root = home / slug
    state = root / '.state'
    repo = state / 'repo.git'
    (state / 'open').mkdir(parents=True, exist_ok=True)
    (state / 'archive').mkdir(exist_ok=True)
    ensure_clone(repo, info['url'])
    fetch_branch(repo, branch)
    load_repo_config(repo, branch)
    if args.max_prs:
        CFG['max_prs'] = args.max_prs
    key_base = config_hash()

    note = ''
    target = None
    if args.target:
        target, note = resolve(args.target, slug)
    elif not args.repo and (out := gh('pr', 'view', '--json', 'number', check=False)):
        target = json.loads(out)['number']

    # What is open now, and what was open at the last sync
    open_prs = json.loads(gh('pr', 'list', '--repo', slug, '--state', 'open', '--limit', str(CFG['max_prs']),
                             '--search', 'sort:updated-desc', '--json', PR_FIELDS))
    capped = len(open_prs) == CFG['max_prs']
    by_num = {p['number']: p for p in open_prs}
    records = {int(p.stem): json.loads(p.read_text()) for p in (state / 'open').glob('*.json')}
    gone = []
    for n in sorted(set(records) | ({target} if target else set())):
        if n in by_num:
            continue
        pr = json.loads(gh('pr', 'view', str(n), '--repo', slug, '--json', PR_FIELDS))
        if pr['state'] == 'OPEN':
            if n == target or not capped:
                open_prs.append(pr)  # the one asked for stays live even past the cap
                by_num[n] = pr
            else:
                (state / 'open' / f'{n}.json').unlink()  # still open, but fell off the most-recent list
                del records[n]
        else:
            gone.append(pr)

    # Analyse the PRs whose commits changed
    def key_of(pr: dict) -> str:
        return f'{key_base}:{pr["headRefOid"]}:{pr["baseRefName"]}'

    todo = [p for p in open_prs + gone
            if args.force or records.get(p['number'], {}).get('key') != key_of(p)]
    todo = [p for p in todo if p['state'] == 'OPEN' or p['number'] == target or p['number'] not in records]
    parsed = Parsed(repo, state / 'imports.json')
    if todo:
        fetch_heads(repo, [p['number'] for p in todo])
        fetch_branch(repo, branch)  # a shallow PR fetch can cut the default branch's history; put it back
    for i, pr in enumerate(todo, 1):
        n = pr['number']
        print(f'Analysing #{n} ({i} of {len(todo)})', file=sys.stderr)
        blobs = source_blobs(repo, f'refs/remotes/pr/{n}')
        parsed.load(blobs)
        records[n] = {'key': key_of(pr), 'number': n, 'analysed': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                      **analyse(slug, pr, Graph(blobs, parsed))}

    # Facts around the PRs, refreshed every sync
    touches, last, commits = history(repo, branch)
    main_sha = run('git', 'rev-parse', f'origin/{branch}', cwd=repo).strip()
    tcache = state / 'thresholds.json'
    T = json.loads(tcache.read_text()) if tcache.is_file() else {}
    if T.get('key') != f'{key_base}:{main_sha}':
        blobs = source_blobs(repo, f'origin/{branch}')
        parsed.load(blobs)
        T = {'key': f'{key_base}:{main_sha}', **thresholds(Graph(blobs, parsed), touches, commits)}
        tcache.write_text(json.dumps(T))
    stacks, related = stacks_of(open_prs)
    facts = {'T': T, 'touches': touches, 'last': last, 'branch': branch, 'stacks': stacks, 'related': related,
             'on_main': set(run('git', 'ls-tree', '-r', '--name-only', f'origin/{branch}', cwd=repo).splitlines()),
             'pr_files': {n: {f['path'] for f in records[n]['files']} for n in by_num}}

    now = datetime.now(timezone.utc)
    stamp = now.strftime('%Y-%m-%d %H:%M UTC')
    common = {'repo': slug, 'url': info['url'], 'branch': branch, 'mapped': stamp, 'kinds': KINDS,
              'areas': {k: a['label'] for k, a in CFG['areas'].items()}}
    for pr in open_prs:
        rec = records[pr['number']]
        rec['view'] = view_of(rec, pr, facts)
        (state / 'open' / f'{pr["number"]}.json').write_text(json.dumps(rec))

    # A PR that merged or closed leaves the live view and is kept in the archive
    archived_now = []
    for pr in gone:
        n = pr['number']
        rec = records.pop(n)
        closed = (pr.get('mergedAt') or pr.get('closedAt') or now.isoformat())[:10]
        view = rec.get('view') if rec['key'] == key_of(pr) and rec.get('view') else view_of(rec, pr, facts)
        view.update(state=pr['state'], title=pr['title'], closed=closed)
        rec.update(view=view, T=T)
        (state / 'archive' / f'{n}.json').write_text(json.dumps(rec))
        (state / 'open' / f'{n}.json').unlink(missing_ok=True)
        write_page(root / 'archive' / closed[:7] / f'pr-{n}.html',
                   {**common, 'thresholds': T, 'prs': [], 'archived': [view], 'only': n},
                   f'PR {n} briefing: {slug}')
        archived_now.append(view)

    cutoff = (now - timedelta(days=CFG['archive_days'])).strftime('%Y-%m-%d')
    recent = [json.loads(p.read_text())['view'] for p in (state / 'archive').glob('*.json')]
    recent = sorted((v for v in recent if v['closed'] >= cutoff), key=lambda v: v['closed'], reverse=True)[:20]
    views = [records[p['number']]['view'] for p in open_prs]
    data = {**common, 'thresholds': T, 'prs': views, 'archived': recent, 'only': None,
            'capped': CFG['max_prs'] if capped else 0, 'archive_days': CFG['archive_days']}
    write_page(root / 'index.html', data, f'PR briefing: {slug}')
    if args.artifact:
        write_page(state / 'artifact.html', data, f'PR briefing: {slug}', body_only=True)

    # Report
    if note:
        print(note)
    done = {p['number'] for p in todo}
    print(f'Synced {slug}: {plural(len(views), "open PR")} ({len([v for v in views if v["number"] in done])} analysed, '
          f'{len([v for v in views if v["number"] not in done])} unchanged), {len(archived_now)} archived'
          + (f'. Showing the {CFG["max_prs"]} most recently updated' if capped else ''))
    for v in views + archived_now:
        watch = [c['t'] for c in v['ledger'] if c['state'] == 'watch']
        held = [c['t'] for c in v['ledger'] if c['state'] == 'held']
        state_note = '' if v['state'] == 'OPEN' else f' [{v["state"].lower()} {v["closed"]}, archived]'
        print(f'#{v["number"]} {v["title"][:70]}{state_note}: {v["mag"]["close"]:,} of {v["mag"]["total"]:,} lines '
              f'to read closely, {len(watch)} to watch, {plural(len(held), "assurance")}')
        if v['number'] == target:
            print('  Watch: ' + ('; '.join(watch) or 'nothing'))
            print('  Assurances: ' + ('; '.join(held) or 'none'))
    page = root / 'index.html'
    print(f'Wrote {page}')
    if target:
        print(f'Briefing for #{target}: {page.as_uri()}#pr-{target}')
    if args.artifact:
        print(f'Artifact page body: {state / "artifact.html"}')
        print(f'Artifact description: Open pull requests in {slug}: magnitude, risks and review order')


if __name__ == '__main__':
    main()
