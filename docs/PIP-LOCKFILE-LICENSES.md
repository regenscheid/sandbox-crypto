<!-- SPDX-License-Identifier: MIT -->
# Pip license declarations in lockfiles

The installed-package snapshot now records an optional `PipDistribution.license`
for distributions installed by pip or uv. This closes the pip license metadata
portion of [CP-16](specs/conda-provisioning.md); it also carries the declaration
through the existing selected-revision package and lockfile comparison readers.
The change records metadata. It neither verifies legal terms nor changes
provisioning permission or the resolver's existing license policy.

The standalone, standard-library-only snapshot reads the existing bounded
`METADATA` or `PKG-INFO` file (including flat legacy `.egg-info`) once. It keeps
one declaration in this order:

1. A unique `License-Expression` header, when present.
2. Otherwise a unique legacy `License` header.
3. Otherwise a single `License ::` classifier, kept as its original classifier
   text rather than mapped to an invented SPDX identifier.

This follows the precedence of [Python core metadata](https://packaging.python.org/en/latest/specifications/core-metadata/).
A present but unusable higher-priority field stays unknown; it does not expose
an older conflicting declaration. Duplicate declarations, multiple license
classifiers, empty/`UNKNOWN` values, invalid UTF-8, malformed headers and values
outside 1–256 printable ASCII characters stay `null`. Header folding is
unfolded to spaces; no first-line truncation or inferred AND/OR choice is made.
Retaining a `License-Expression` does not validate its SPDX identifiers or syntax.
The field applies to the recorded distribution metadata, not every artifact or
license obligation of the broader project.

Metadata files retain the existing 64 KiB read cap and final-component
`O_NOFOLLOW`, nonblocking, regular-file checks. The snapshot does not import a
distribution, execute setup hooks, process `.pth` files or open `License-File`
paths. This is the existing trusted environment snapshot path, not a new
filesystem sandbox: parent directory replacement and malicious metadata are
not authenticated by a declaration. No extra raw metadata or license-file body
is retained. Existing name/version fallback and Conda-owned exclusion remain.

Lockfile format/version remain `sandbox-science-lockfile`/1. Saved raw lockfiles
and their hashes are never rewritten or backfilled. Old entries without the
field parse as unknown; absent and explicit `null` have the same package
comparison meaning. A new snapshot includes the nullable field. The existing
fingerprint/revision detection is unchanged: editing only an existing metadata
file does not promise a new revision. The next snapshot records what it reads.

The selected package and difference rows bind their declared license to the
retained pip entry. Known text uses the existing AGPL/Affero text flag; unknown
text keeps `agpl=null`. Shared page validation binds the flag to that same
text heuristic, refusing a copied/inverted flag on either comparison side.
This flag is a text observation, not an SPDX evaluator,
verified license, or install permission. The existing GUI decoder accepts
legacy absent fields and new bounded declarations without loosening unrelated
field validation. Scope, pins, paging, deadlines and request ownership are
unchanged. No new route, install, restore, approval or wake effect is added.

Validation uses actual disposable metadata files and retained synthetic blobs,
with no package installation or external service: modern/legacy/folded headers,
precedence, duplicates, bounds, UTF-8, ignored bodies/license paths, final-file
symlink/FIFO/directory refusal, non-execution sentinels, old raw-byte preservation,
license-only comparison, selected-page coherence and strict GUI compatibility.
Independent review, root integration/full gates and any real runtime acceptance
are separate from these component checks.
