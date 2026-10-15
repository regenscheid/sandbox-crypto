# Skills

Built: importing skill packs from git or an archive, and pinning them in projects ([skills](specs/skills.md), SK-1 to SK-6, SK-32). Runs do not see skills yet: the index, the `skills` tool and discovery come next.

A skill is a directory with a `SKILL.md`: front matter with a `name` and a `description`, then a body. A pack is a set of skills, such as a repository's `skills/` directory. Each pack is imported under a source name, and a skill's qualified ID is `<source>/<name>`.

## Importing a pack

The user imports a pack on their own machine with the harness:

```sh
sandbox-science-harness skills import --state-dir STATE --name crypto \
  --git https://github.com/OWNER/REPO [--ref BRANCH|TAG|COMMIT] [--path DIR]
sandbox-science-harness skills import --state-dir STATE --name mine --archive pack.tar.gz
```

- **From git.** `--git` takes a URL or a local repository's path. The harness fetches one commit, the ref's or the repository's default branch's, into a temporary bare repository, with the user's git, configuration and credentials, and records the resolved commit. A commit is named by its full hash. Files are read from git's objects, so nothing in the pack is written out as a file. The platform's services and the sandboxes never fetch a pack.
- **From an archive.** `--archive` takes a `.tar`, `.tar.gz` or `.zip` file. It is read in memory, without the single top directory every entry is in, as in GitHub's archives, unless that directory is a skill's own. The import records the archive's SHA-256.
- **Which files.** The skills are the directories directly under `skills/` when the pack has skills there, and directly under its top otherwise, or under `--path`. Only that directory's files are kept, with paths relative to it, so sibling links such as `../alpha/SKILL.md` stay inside the pack. Symbolic links, submodules and other entries that are not regular files are left out, and so are paths that are not plain relative ones and files over 1 MiB. A pack over 4,096 files or 32 MiB is refused.
- **Which skills.** Front matter is YAML between `---` lines at the start of `SKILL.md`, read with the safe loader and without aliases. A skill is excluded, with the reason, when its `name` is missing, is not lowercase letters, digits and hyphens (at most 64), or differs from its directory's name; when its `description` is missing or over 1,024 characters; or when its body is over 64 KiB. Its other front-matter fields are kept as metadata. Nothing in a pack is run.
- **The report.** The command prints the import as JSON: the source, origin, ref, commit or archive digest, the skills' directory, the snapshot's SHA-256, and a report of the skills, the excluded skills and the files left out, each with its reason; the default branch when no ref was given; and, when the source was imported before, the skills added, removed and changed since (a skill changes when any file in its directory does). The same import again, from the same place at the same revision, adds nothing and says `"unchanged": true`.

## Pinning

```sh
sandbox-science-harness skills pin --state-dir STATE --project P --source crypto [--revision 4089e99]
sandbox-science-harness skills list --state-dir STATE [--project P | --source crypto]
```

A project pins one import of each source it uses: the source's latest, or the one whose commit, archive digest or snapshot starts with `--revision`. Pinning another import of the source moves the pin, and its revision counts the moves. A pin is refused when one of its skills has the name of a skill of another source the project pins; the refusal names the skills and the source (SK-4). `skills list` lists the imports, the latest first, or a source's, or a project's pins with their skills.

## Where it is kept

In the control store (`control.sqlite3`), written once: `skill_blobs` holds each file's content by its SHA-256, shared by every snapshot; `skill_snapshots` and `skill_files` hold each snapshot's files, a snapshot being named by a SHA-256 over its paths, whether each is executable and their contents' SHA-256; `skill_skills` holds each snapshot's skills with their descriptions, body SHA-256 and metadata; and `skill_imports` holds each import with its report. `project_skill_pins` holds each project's pins.

Code: `packages/orchestrator/src/sandbox_science/orchestrator/skills/` and `cli_skills.py`.
