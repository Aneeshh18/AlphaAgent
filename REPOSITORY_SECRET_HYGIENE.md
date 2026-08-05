# Repository and release secret hygiene

AIOS has a path-metadata release gate. It prevents known local tooling,
credential-store, private-key, environment-secret, and runtime-state paths from
entering the current Git index or a reviewed publication artifact. It does not
read file payloads.

Run the index gate before every commit:

```bash
.venv/bin/python scripts/verify_repository_hygiene.py --git-index
```

Run it again against every freshly built wheel and source distribution:

```bash
.venv/bin/python scripts/verify_repository_hygiene.py \
  --artifact dist/ai_investment_os-VERSION-py3-none-any.whl \
  --artifact dist/ai_investment_os-VERSION.tar.gz
```

If a publication process assembles an unpacked directory, inspect that staging
directory rather than the working repository:

```bash
.venv/bin/python scripts/verify_repository_hygiene.py \
  --tree PATH_TO_PUBLICATION_STAGING
```

The working repository intentionally contains ignored local `data/` and `logs/`
directories, so it is not itself a publication tree. JSON output is available
with `--json`; it contains only policy, target counts, paths, and reason codes.

## Policy boundary

The verifier inspects only:

- cached Git names and modes from `git ls-files`;
- publication-tree entry names and filesystem types; and
- ZIP, wheel, TAR, and source-distribution member names and types.

It rejects:

- every `.zcode/` subtree;
- `.env` and non-template `.env.*` files;
- private-key-shaped names and private-key container extensions;
- workstation credential stores such as AWS, Google Cloud, Docker, Kubernetes,
  SSH, package-registry, and generic credential/token files;
- runtime data, log, backup, snapshot, DuckDB, SQLite, database, WAL, dump, and
  backup paths; and
- Git modes other than reviewed regular `100644`/`100755` files, conflict
  stages, and duplicate staged paths; and
- ambiguous or symlink-aliased targets, duplicate archive members, links,
  hardlinked publication files, and special file types whose target or payload
  cannot be established from safe metadata.

The safe environment-template names are `.env.example`, `.env.sample`,
`.env.template`, and `.env.dist`. Public certificate names such as
`docs/public-ca.pem` remain allowed; `.pem` is not treated as a private key
without a private-key-shaped filename. Public synthetic runtime-format fixtures
may live only below `tests/fixtures/public/`. Secret-shaped filenames still fail
there.

`.gitignore` reduces accidental staging, Hatch exclusions reduce accidental
packaging, and the verifier is the acceptance boundary. Ignore rules alone do
not remove already tracked files. The pinned CI workflow runs the cached-index
gate with read-only repository permission and without persisting checkout
credentials.

## What this does not prove

This gate is not a content secret scanner. A credential stored under a
misleading ordinary filename is outside its path-only evidence. It also does
not prove that a value was never committed, remove public Git history, revoke a
credential, invalidate a fork or cache, or replace GitHub secret scanning and
push protection.

Before making a repository public, the repository owner must separately:

1. rotate and revoke every credential or private key that may have entered any
   commit, release, CI artifact, log, cache, or shared clone;
2. inventory affected history and refs with an approved secret-scanning service
   without copying findings into tickets or chat;
3. obtain explicit approval and coordinate a push freeze before any destructive
   history rewrite;
4. address forks, mirrors, cached archives, release assets, CI artifacts, and
   support-provider retention, recognizing that copies may remain;
5. validate the rewritten repository in a disposable clone before an approved
   force push; and
6. enable repository secret scanning, push protection, least-privilege CI
   tokens, protected branches, and recurring rotation.

Rotation comes first because history rewriting cannot make an exposed
credential trustworthy again.
