"""Shell-style word splitting shared by processors.

The browser CLI sends ``positionals``: the arguments after the command and
sub-command words, split like a shell (quotes removed, flags excluded). Older
browsers only send ``value`` as typed, so :func:`get_positionals` falls back to
splitting it with the same rules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cli_process_command import CliProcessCommand


def split_shell_words(text: str) -> list[str]:
    """Split ``text`` into words like a shell does.

    - whitespace separates words; quotes group (``"a b"`` -> ``a b``) and are removed
    - single quotes are literal; inside double quotes ``\\"`` and ``\\\\`` are escapes
    - outside quotes a backslash escapes the next character (``my\\ file`` -> ``my file``)
    - a quote without a closing one is plain text
    """
    words: list[str] = []
    current = ""
    in_word = False
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            current += text[i + 1]
            in_word = True
            i += 2
            continue
        if ch == "'":
            end = text.find("'", i + 1)
            if end != -1:
                current += text[i + 1 : end]
                in_word = True
                i = end + 1
                continue
        elif ch == '"':
            buf = ""
            j = i + 1
            closed = False
            while j < n:
                c = text[j]
                if c == "\\" and j + 1 < n and text[j + 1] in ('"', "\\"):
                    buf += text[j + 1]
                    j += 2
                elif c == '"':
                    closed = True
                    break
                else:
                    buf += c
                    j += 1
            if closed:
                current += buf
                in_word = True
                i = j + 1
                continue
        if ch.isspace():
            if in_word:
                words.append(current)
            current = ""
            in_word = False
        else:
            current += ch
            in_word = True
        i += 1
    if in_word:
        words.append(current)
    return words


def join_shell_words(text: str) -> str:
    """The arguments as a shell's echo prints them: the words of
    :func:`split_shell_words` joined by one space (``"a" "b"`` -> ``a b``)."""
    return " ".join(split_shell_words(text))


def get_positionals(command: "CliProcessCommand") -> list[str]:
    """The command's positional arguments, unquoted: ``command.positionals``
    when the client sent them, otherwise ``command.value`` split like a shell."""
    positionals = getattr(command, "positionals", None)
    if positionals is not None:
        return [str(p) for p in positionals]
    return split_shell_words(command.value or "")


def get_positional_text(command: "CliProcessCommand") -> str:
    """The positional arguments joined by one space (``""`` when there are none)."""
    return " ".join(get_positionals(command))
