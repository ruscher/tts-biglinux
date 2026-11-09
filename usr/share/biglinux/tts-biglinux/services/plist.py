"""
Property lists of Window Maker (WINGs) and Wayland Maker (wlmaker).

Just enough to edit one entry of a file the user owns without touching the
rest: every parsed value remembers where it is in the text (``start``/``end``),
so an entry is inserted or removed by splicing text, and comments and layout
stay byte for byte. ``check`` re-parses a result with the rules of the program
that will read it, before anything is written.

The wlmaker dialect follows its parser (libbase): ``//`` comments only, no
``<data>``, unquoted strings limited to ``[A-Za-z0-9_.$]``. An invalid
Config.plist stops wlmaker at login, so nothing that fails here is written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_BARE = re.compile(r'[^\s{}()=;,"<>]+')
_WLMAKER_BARE = re.compile(r"[A-Za-z0-9_.$]+")
_WINGS_ESC = {"a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r", "e": "\x1b"}


class PlistError(ValueError):
    """The text is not a valid property list for the program that reads it."""


@dataclass
class Node:
    kind: str  # "str" | "data" | "array" | "dict"
    start: int
    end: int = 0
    value: object = None  # str for "str", bytes for "data"
    quoted: bool = False
    items: list["Node"] = field(default_factory=list)  # array
    entries: list["Entry"] = field(default_factory=list)  # dict

    def to_py(self):
        if self.kind in ("str", "data"):
            return self.value
        if self.kind == "array":
            return [i.to_py() for i in self.items]
        return {e.key.value: e.value.to_py() for e in self.entries}

    def entry(self, key: str) -> "Entry | None":
        for e in self.entries:
            if e.key.value == key:
                return e
        return None


@dataclass
class Entry:
    key: Node
    value: Node
    start: int  # the key's first character
    end: int  # after the ";" (or the value, for a last entry without it)


class _Parser:
    def __init__(self, text: str, dialect: str) -> None:
        self.t = text
        self.i = 0
        self.dialect = dialect

    def error(self, msg: str):
        line = self.t.count("\n", 0, self.i) + 1
        raise PlistError(f"line {line}: {msg}")

    def skip(self) -> None:
        t = self.t
        while self.i < len(t):
            c = t[self.i]
            if c.isspace():
                self.i += 1
            elif t.startswith("//", self.i):
                j = t.find("\n", self.i)
                self.i = len(t) if j < 0 else j + 1
            elif t.startswith("/*", self.i):
                if self.dialect == "wlmaker":
                    self.error("wlmaker does not accept /* */ comments")
                j = t.find("*/", self.i + 2)
                if j < 0:
                    self.error("unterminated /* comment")
                self.i = j + 2
            else:
                break

    def peek(self) -> str:
        self.skip()
        return self.t[self.i] if self.i < len(self.t) else ""

    def expect(self, c: str) -> None:
        if self.peek() != c:
            self.error(f"expected {c!r}, found {self.peek() or 'end'!r}")
        self.i += 1

    def value(self) -> Node:
        c = self.peek()
        start = self.i
        if c == "{":
            return self.dict()
        if c == "(":
            return self.array()
        if c == '"':
            return self.quoted()
        if c == "<":
            if self.dialect == "wlmaker":
                self.error("wlmaker does not accept <data>")
            j = self.t.find(">", self.i)
            if j < 0:
                self.error("unterminated <data>")
            try:
                data = bytes.fromhex(re.sub(r"\s", "", self.t[self.i + 1:j]))
            except ValueError:
                self.error("invalid <data>")
            self.i = j + 1
            return Node("data", start, self.i, data)
        m = _BARE.match(self.t, self.i)
        if not m:
            self.error(f"invalid value {c or 'end'!r}")
        if self.dialect == "wlmaker" and not _WLMAKER_BARE.fullmatch(m.group()):
            self.error(f"{m.group()!r} needs quotes for wlmaker")
        self.i = m.end()
        return Node("str", start, self.i, m.group())

    def quoted(self) -> Node:
        t, start, i, out = self.t, self.i, self.i + 1, []
        while True:
            if i >= len(t):
                self.error("unterminated string")
            c = t[i]
            if c == '"':
                self.i = i + 1
                return Node("str", start, self.i, "".join(out), quoted=True)
            if c == "\\" and i + 1 < len(t):
                n = t[i + 1]
                if self.dialect == "wings" and n in "01234567":
                    m = re.compile(r"[0-7]{1,3}").match(t, i + 1)
                    out.append(chr(int(m.group(), 8)))
                    i = m.end()
                    continue
                out.append(_WINGS_ESC.get(n, n) if self.dialect == "wings" else n)
                i += 2
                continue
            out.append(c)
            i += 1

    def dict(self) -> Node:
        node = Node("dict", self.i)
        self.i += 1
        while self.peek() != "}":
            if not self.peek():
                self.error("dictionary without }")
            key = self.value()
            if key.kind != "str":
                self.error("invalid dictionary key")
            self.expect("=")
            val = self.value()
            end = val.end
            if self.peek() == ";":
                self.i += 1
                end = self.i
            elif self.peek() != "}":
                self.error(f"expected ';' after {key.value!r}")
            node.entries.append(Entry(key, val, key.start, end))
        self.i += 1
        node.end = self.i
        return node

    def array(self) -> Node:
        node = Node("array", self.i)
        self.i += 1
        while self.peek() != ")":
            if not self.peek():
                self.error("array without )")
            node.items.append(self.value())
            if self.peek() == ",":
                self.i += 1
            elif self.peek() != ")":
                self.error("expected ',' or ')'")
        self.i += 1
        node.end = self.i
        return node

    def document(self) -> Node | None:
        if not self.peek():
            return None
        node = self.value()
        if self.peek():
            self.error("text after the end of the document")
        return node


def parse(text: str, dialect: str = "wings") -> Node | None:
    """Parse ``text`` ("wings" or "wlmaker" rules); raises PlistError."""
    if dialect not in ("wings", "wlmaker"):
        raise ValueError(dialect)
    return _Parser(text, dialect).document()


def check(text: str, dialect: str) -> Node:
    """Parse and require a document (the empty text is not a valid file)."""
    root = parse(text, dialect)
    if root is None:
        raise PlistError("empty document")
    return root


def quote(s: str) -> str:
    """A string as both dialects read it back unchanged."""
    if _WLMAKER_BARE.fullmatch(s):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def line_start(text: str, pos: int) -> int:
    return text.rfind("\n", 0, pos) + 1


def line_indent(text: str, pos: int) -> str:
    start = line_start(text, pos)
    m = re.compile(r"[ \t]*").match(text, start)
    return m.group() if m else ""
