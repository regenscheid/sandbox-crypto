# Generated skill binding inventory

Implementation contract, 2026-10-17 (RC-23/RC-24/F15).

The research image and executor use the same standard-library static scanner. It
enumerates Conda, pip and distribution-owned files and records exact variable reads,
command namespaces and reviewed fallback families. Each generated restriction names
its package/version, relative extraction source, rule and source digest. Staging
returns matching provenance with the current manifest digest. Canonical
sorted JSON has a format/extractor version and content SHA-256; observation time is
separate. No helper, installed module or tool is executed and no environment values
are read. Extraction covers Python/shell/C-style literal reads and reviewed package families.
ELF files with getenv symbols contribute conservative null-delimited underscore
names; bare/computed names, documentation, translations and build headers remain
covered by the maintained fallback rules. Unsupported accesses and skipped large files are explicit coverage
notes, not claims of exhaustive inference. Enumeration/read/total-size failures make
the inventory unavailable rather than publish a truncated authoritative result.

The executor generates against its current package fingerprint and package epoch,
checks the fingerprint again before publication, and caches only that version.
Staging receives the current inventory and provenance digest. A package change or
executor replacement requires regeneration. Shell, job and cell requests from staged
skills carry that inventory digest; a changed digest requires reloading the skills.
Reloading revalidates every loaded pack, so refreshing one cannot bless an older
pack's stale automatic namespace. Trusted direct worker requests still receive the
current generated-variable checks even without a staging token. Missing, malformed or failed generation
holds all skill bindings; skill text and helper-file staging remain available.
Existing maintained denials remain unconditional and generated names supplement them.
A namespace or explicit approval grants no exception for a reserved variable.

Every binding-bearing shell/job launch and kernel start/cell checks the current
inventory at the existing launch serialization boundary. Cached harness bindings
therefore cannot bypass a package change. A surviving kernel whose inherited skill
inventory changes is refused with a request to restart it; filtering a later
request cannot erase its inherited environment. Ordinary executions without skill
bindings remain available. This does not turn mutable guest observations into a
security boundary against arbitrary research code changing its own environment.

Deterministic tests cover repeated generation, additions/upgrades, provenance,
coverage failures, existing and generated denials, ordinary bindings, staging,
package changes after staging and inherited kernel bindings. OpenShell/image-build
validation remains separately reported.

Bounds: one metadata file is at most 16 MiB; one scanned program at most 2 MiB;
aggregate scanned program input at most 2 GiB; at most 300,000 candidate files,
20,000 packages, 100,000 package/name restrictions and 64 MiB encoded manifest.
Exceeded aggregate bounds refuse publication. Oversized program files are declared
outside the extraction policy in coverage notes. The cache is in process and rebuilt
on executor replacement; a guest-written manifest is never loaded as authority.
