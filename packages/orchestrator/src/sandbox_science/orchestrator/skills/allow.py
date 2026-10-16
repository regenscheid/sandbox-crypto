# SPDX-License-Identifier: MIT
"""Which derived bindings a run may set (docs/specs/skills.md, SK-31, SK-16; the owner's
decision of 2026-10-16).

A pack's own files can mention any variable, so neither the mention rule nor a deny-list
can stop a hostile pack: the set of variables that make some tool load a file is
open-ended. A binding is therefore set only when it is namespaced to the pack, its name
starting with the source's name in capitals (``crypto-tools`` gives ``CRYPTO_TOOLS_``) or
with a prefix the project's user set for the source, or when the project's user approved
it. Any other is held, with its reason, until approved. The deny-list stays as the
backstop: a variable on it is never set, namespaced or approved.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from typing import Final

PREFIX: Final = re.compile(r"[A-Z][A-Z0-9_]{1,30}_")
"""A prefix the user sets for a source: at least three characters, ending in ``_``."""

FORBIDDEN: Final = frozenset(
    {
        "PATH", "HOME", "SHELL", "USER", "LOGNAME", "LANG", "LANGUAGE", "TERM", "TMPDIR",
        "TMP", "TEMP", "PWD", "OLDPWD", "IFS", "ENV", "CDPATH", "MAIL", "HOSTNAME", "TZ",
        "EDITOR", "VISUAL", "PAGER", "MANPAGER", "BROWSER", "MANPATH", "INFOPATH",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "REQUESTS_CA_BUNDLE",
        "CLASSPATH", "VIRTUAL_ENV", "IPYTHONDIR", "DOT_SAGE", "GPRC", "ZDOTDIR",
        "CONFIG_SITE", "CC", "CXX", "CPP", "LD", "AR", "AS", "NM", "RANLIB", "FC", "F77",
        "DISPLAY", "INPUTRC", "HISTFILE", "COLUMNS", "LINES", "MATPLOTLIBRC", "MPLCONFIGDIR",
        "MPLBACKEND", "TERMINFO", "TERMCAP", "EMACS", "SUDO_ASKPASS", "GIT_ASKPASS",
        "SSH_ASKPASS", "LS_COLORS", "TEXINPUTS", "BIBINPUTS", "TEXMFHOME", "WGETRC",
        "CURL_HOME", "NETRC", "KRB5CCNAME", "GNUPGHOME", "MAKEFILES",
    }
)  # fmt: skip
"""Shell, loader, interpreter start-up, pager and tool-configuration variables a binding
may not name (SK-31): a pack's file under one would run, or change what runs, at every
start of the tool."""
FORBIDDEN_PREFIXES: Final = (
    "LD_", "DYLD_", "PYTHON", "PY", "IPYTHON", "JUPYTER", "SAGE", "PARI", "GP_", "R_",
    "RSTUDIO", "JULIA", "MATLAB", "OCTAVE", "MAGMA", "MAPLE", "MATHEMATICA", "PERL",
    "RUBY", "GEM_", "BUNDLE", "NODE", "NPM_", "YARN", "DENO", "BUN_", "JAVA", "JDK_",
    "MAVEN", "GRADLE", "RUST", "CARGO", "GO", "CONDA", "MAMBA", "PIP_", "UV_", "POETRY",
    "VIRTUALENV", "TCL", "TK_", "LC_", "BASH", "ZSH", "SHELL", "PS", "PROMPT", "HIST",
    "LESS", "MORE", "GIT_", "SSH_", "SSL_", "OPENSSL", "CURL", "GNUTLS", "XDG_", "DBUS_",
    "WAYLAND", "X", "QT_", "GTK", "MALLOC", "GCONV", "NLSPATH", "LOCPATH", "HOSTALIASES",
    "RESOLV", "SANDBOX_", "OMP_", "MKL_", "OPENBLAS", "CUDA", "NV", "LIBRARY_PATH",
    "CPATH", "C_INCLUDE", "CPLUS_INCLUDE", "PKG_CONFIG", "CMAKE", "MAKE", "TEX", "MPL",
    "KRB5", "GNUPG", "GPG", "AWS_", "GOOGLE_", "AZURE_", "KUBE", "DOCKER", "MAXIMA",
    "SINGULAR", "ECL", "LUA", "CCACHE", "GIO_", "GI_", "GST_", "GDK", "GSETTINGS", "LIBGL",
    "MESA", "VK_", "SASL", "MAGICK", "FONTCONFIG", "NLTK", "HF_", "TRANSFORMERS", "TORCH",
    "TF_", "ENCHANT", "ASPELL", "HUNSPELL", "PROJ_", "GDAL", "SPARK", "HADOOP", "QT",
)  # fmt: skip
FORBIDDEN_SUFFIXES: Final = (
    "OPT", "OPTS", "OPTIONS", "FLAGS", "RC", "RCFILE", "PROFILE", "PROFILE_USER",
    "STARTUP", "STARTUP_FILE", "ENVIRON", "ENVIRON_USER", "WRAPPER", "HOOK", "HOOKS",
    "INIT", "_CONFIG", "_CONFIG_DIR", "CONFIG_PATH", "_LIBS", "_PLUGINS",
)  # fmt: skip
"""Families of start-up and configuration variables, whatever the tool."""
TOOL_FAMILIES: Final = (
    "GAP_", "GCC_", "CC_", "HDF5_", "GLIBC_", "OMPI_", "OPAL_", "PMIX_", "MPI_", "PHP_",
    "SYSTEMD_", "SUDO_", "MYSQL_", "GS_", "TERMINFO", "SESSION_", "PROJECT_", "LIBVA_",
    "OCL_", "FOURTITWO_", "JMOL_", "NTL_", "GH_", "GUILE_", "CLOUDSDK_", "COVERAGE_",
    "RUBIKS_", "FLINT_", "GIAC_", "LCALC_", "SYMPOW_", "PALP_", "POLYMAKE_", "M2_",
    "MACAULAY2_", "CDD_", "NORMALIZ_", "LATTE_", "TACHYON_", "GIVARO_", "LINBOX_", "FFLAS_",
    "MPFR_", "GMP_", "ECLIB_", "GLPK_", "CBC_", "SCIP_", "CPLEX_", "GUROBI_", "MOSEK_",
    "FRICAS_", "AXIOM_", "MUPAD_", "REDUCE_", "PARIGP_", "GPHELP", "ARB_", "MPC_", "PPL_",
    "BOOST_", "OPENSSH_", "WGET", "RSYNC_", "TAR_", "GZIP", "BZIP", "XZ_", "ZSTD_", "LZ4_",
    "SQLITE_", "PG", "PSQL", "REDIS", "MONGO", "NPY_", "NUMPY", "SCIPY", "SYMPY", "NUMBA_",
    "CUPY_", "JAX_", "XLA_", "KERAS_", "MKL", "BLAS", "LAPACK", "ATLAS_", "SPACK_", "NIX_",
    "BREW_", "HOMEBREW_", "APT_", "DEBIAN_", "DPKG_", "RPM_", "YUM_", "DNF_", "SNAP_",
    "FLATPAK_", "APPTAINER", "SINGULARITY", "PODMAN", "CONTAINER", "BUILDAH", "OPENSHELL",
    "LITELLM", "ANTHROPIC", "OPENAI", "HF", "WANDB_", "MLFLOW_", "RAY_", "DASK_", "SPHINX",
    "MAKEINFO", "TEXMF", "LUATEX", "PDFTEX", "XETEX", "PANDOC", "LATEXMK", "BIBTEX", "BIBER",
    "GHOSTSCRIPT", "MAGMA", "MATHEMATICA", "WOLFRAM", "SVN_", "HG", "CVS", "BZR_", "FOSSIL_",
)  # fmt: skip
"""Tools' own variable families that a pack's namespace would claim: lab 43's review found a
pack named ``gap`` binding ``GAP_ROOT_PATHS``, whose ``init.g`` Sage's libgap ran, and one
named ``gcc`` binding ``GCC_EXEC_PREFIX``, whose ``cc1`` ran. They are on the deny-list,
and no source or prefix has them for a namespace."""
MIN_NAMESPACE: Final = 3
"""A namespace's characters at least, its ``_`` included."""


def forbidden(variable: str) -> bool:
    """Whether a binding may not name ``variable``, by name or by family."""
    return (
        variable in FORBIDDEN
        or variable.startswith(FORBIDDEN_PREFIXES + TOOL_FAMILIES)
        or variable.endswith(FORBIDDEN_SUFFIXES)
    )


def namespace(source: str) -> str:
    """The prefix a source's variables carry without asking: its name in capitals."""
    return source.upper().replace("-", "_") + "_"


def taken(name: str, tools: Collection[str] = ()) -> str | None:
    """Why ``name``, a namespace, belongs to something else, or ``None``: too short, a
    family on the deny-list, or the name of a command ``tools`` has (the environment's
    ``PATH``, as the worker sees it), whole or by its first part."""
    if len(name) < MIN_NAMESPACE:
        return f"{name} is shorter than {MIN_NAMESPACE} characters"
    if forbidden(name + "X"):
        return f"{name} is a family of shell, loader, start-up or a tool's own variables"
    for command in commands(name):
        if command in tools:
            return f"{name} is the namespace of the command {command}'s own variables"
    return None


def commands(name: str) -> list[str]:
    """The commands whose variables the namespace ``name`` would be: its name in lower
    case, with ``_`` or ``-``, and its first part."""
    stem = name.rstrip("_").lower()
    return sorted({stem, stem.replace("_", "-"), stem.split("_")[0]} - {""})


def prefix_refused(prefix: str) -> str | None:
    """Why a source may not be given ``prefix``, or ``None``."""
    if not PREFIX.fullmatch(prefix):
        return f"{prefix[:40]!r} is no prefix: capitals, digits and _, 3 to 32, ending in _"
    return taken(prefix)


def granted(
    source: str, prefix: str | None, others: Collection[str], tools: Collection[str] = ()
) -> tuple[list[str], list[str]]:
    """The namespaces ``source``'s variables are set under, its name's and ``prefix``, and
    why any is refused: taken (``taken``), or a prefix of, or the same as, a namespace of
    another source in ``others``, whose variables it would claim."""
    names: list[str] = []
    refused: list[str] = []
    for name in (namespace(source), *([prefix] if prefix else [])):
        if why := taken(name, tools):
            refused.append(why)
        elif claims := sorted(other for other in others if other.startswith(name)):
            refused.append(f"{name} would claim another source's namespace {claims[0]}")
        else:
            names.append(name)
    return names, refused


def held(
    variable: str,
    source: str,
    *,
    names: Sequence[str],
    refused: Sequence[str] = (),
    approved: Collection[str] = (),
) -> str | None:
    """Why the derived binding of ``variable`` for ``source`` is not set, or ``None`` when
    it is: under one of its namespaces ``names`` (``granted``), or ``approved``."""
    if forbidden(variable):
        return "a shell, loader, start-up or a tool's own variable: never set"
    if variable.startswith(tuple(names)) or variable in approved:
        return None
    why = f"not namespaced to the pack ({', '.join(names) or 'none'})"
    if refused:
        why += f"; {'; '.join(refused)}"
    return (
        f"{why}: held until the project's user approves it, with skills override "
        f"--source {source} --approve {variable}"
    )
