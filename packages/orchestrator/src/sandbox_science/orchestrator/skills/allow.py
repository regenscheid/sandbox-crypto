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


def forbidden(variable: str) -> bool:
    """Whether a binding may not name ``variable``, by name or by family."""
    return (
        variable in FORBIDDEN
        or variable.startswith(FORBIDDEN_PREFIXES)
        or variable.endswith(FORBIDDEN_SUFFIXES)
    )


def namespace(source: str) -> str:
    """The prefix a source's variables carry without asking: its name in capitals."""
    return source.upper().replace("-", "_") + "_"


def prefix_refused(prefix: str) -> str | None:
    """Why a source may not be given ``prefix``, or ``None``."""
    if not PREFIX.fullmatch(prefix):
        return f"{prefix[:40]!r} is no prefix: capitals, digits and _, 3 to 32, ending in _"
    if forbidden(prefix + "X"):
        return f"{prefix} is a prefix of shell, loader or tool-configuration variables"
    return None


def held(
    variable: str, source: str, *, prefix: str | None = None, approved: frozenset[str] = frozenset()
) -> str | None:
    """Why the derived binding of ``variable`` for ``source`` is not set, or ``None`` when
    it is: namespaced to the pack, by its name or ``prefix``, or ``approved``."""
    if forbidden(variable):
        return "a shell, loader, start-up or tool-configuration variable: never set"
    names = namespace(source) + (f" or {prefix}" if prefix else "")
    if variable.startswith(namespace(source)) or (prefix and variable.startswith(prefix)):
        return None
    if variable in approved:
        return None
    return (
        f"not namespaced to the pack ({names}): held until the project's user approves it, "
        f"with skills override --source {source} --approve {variable}"
    )
