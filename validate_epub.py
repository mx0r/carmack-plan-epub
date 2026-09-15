#!/usr/bin/env python3
"""
Structural checks for the generated EPUB. Exits non-zero on any failure.

    python3 validate_epub.py [dist/carmack-plan.epub]

The namespace URIs below are written out as literals on purpose. An earlier
version of the builder emitted the package document in a namespace that does
not exist, and a checker that imported the builder's own constants happily
validated it -- the book was self-consistently wrong, and Apple Books refused
to open it. Hardcoding the spec strings is what makes this check meaningful.
"""

from __future__ import annotations

import re
import sys
import zipfile
from xml.etree import ElementTree as ET

OPF = "http://www.idpf.org/2007/opf"
XHTML = "http://www.w3.org/1999/xhtml"
OPS = "http://www.idpf.org/2007/ops"
NCX = "http://www.daisy.org/z3986/2005/ncx/"
CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container"
DC = "http://purl.org/dc/elements/1.1/"

# EPUB Structural Semantics Vocabulary terms this book is allowed to use.
SSV = {
    "cover", "titlepage", "frontmatter", "bodymatter", "backmatter",
    "preface", "colophon", "chapter", "subchapter", "title", "toc", "landmarks",
}

failures: list[str] = []


def check(label: str, ok: object) -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)
    return bool(ok)


def main(path: str) -> int:
    z = zipfile.ZipFile(path)
    infos = z.infolist()
    names = {n[len("OEBPS/"):] for n in z.namelist() if n.startswith("OEBPS/")}

    print("container and packaging")
    check("zip integrity", z.testzip() is None)
    check("mimetype is first entry, stored uncompressed",
          infos[0].filename == "mimetype"
          and infos[0].compress_type == zipfile.ZIP_STORED
          and z.read("mimetype") == b"application/epub+zip")
    container = ET.fromstring(z.read("META-INF/container.xml"))
    check("container root namespace", container.tag == f"{{{CONTAINER}}}container")

    print("\npackage document")
    pkg = ET.fromstring(z.read("OEBPS/content.opf"))
    check("package root namespace", pkg.tag == f"{{{OPF}}}package")
    check("version 3.0", pkg.get("version") == "3.0")
    check("dc:identifier present",
          pkg.find(f"{{{OPF}}}metadata/{{{DC}}}identifier") is not None)
    check("dc:title present", pkg.find(f"{{{OPF}}}metadata/{{{DC}}}title") is not None)
    check("dcterms:modified present",
          any(m.get("property") == "dcterms:modified" for m in pkg.iter(f"{{{OPF}}}meta")))

    items = list(pkg.iter(f"{{{OPF}}}item"))
    ids = {i.get("id") for i in items}
    hrefs = {i.get("href") for i in items}
    check("manifest is non-empty", len(items) > 0)
    check("manifest ids unique", len(ids) == len(items))
    check("manifest matches zip contents", hrefs | {"content.opf"} == names)
    check("exactly one nav document", sum(i.get("properties") == "nav" for i in items) == 1)
    check("exactly one cover-image",
          sum(i.get("properties") == "cover-image" for i in items) == 1)
    refs = [r.get("idref") for r in pkg.iter(f"{{{OPF}}}itemref")]
    check(f"spine ({len(refs)} items) fully resolves", refs and all(r in ids for r in refs))

    print("\ncontent documents")
    bad_ns, artifacts, replacement = [], [], []
    terms: dict[str, int] = {}
    for name in sorted(n for n in names if n.endswith(".xhtml")):
        raw = z.read("OEBPS/" + name).decode("utf-8")
        root = ET.fromstring(raw)
        if root.tag != f"{{{XHTML}}}html":
            bad_ns.append(name)
        if re.search(r"\[/?code\]", raw, re.I):
            artifacts.append(name)
        if "�" in raw:
            replacement.append(name)
        for el in root.iter():
            for term in (el.get(f"{{{OPS}}}type") or "").split():
                terms[term] = terms.get(term, 0) + 1
    check("all content documents in the XHTML namespace", not bad_ns)
    check("no leftover [code] markup from the archive", not artifacts)
    check("no U+FFFD replacement characters (encoding)", not replacement)

    print("\nstructural semantics")
    print(f"        terms: {dict(sorted(terms.items()))}")
    check("all epub:type terms are valid SSV", set(terms) <= SSV,)
    check("at least one chapter", terms.get("chapter", 0) > 0)
    check("at least one subchapter", terms.get("subchapter", 0) > 0)
    check("front, body and back matter all declared",
          all(terms.get(t, 0) > 0 for t in ("frontmatter", "bodymatter", "backmatter")))

    print("\nnavigation")
    nav = z.read("OEBPS/nav.xhtml").decode("utf-8")
    broken = []
    links = re.findall(r'href="([^"#]+)(?:#([^"]+))?"', nav)
    for target, frag in links:
        if target not in names:
            broken.append(target)
        elif frag and f'id="{frag}"' not in z.read("OEBPS/" + target).decode("utf-8"):
            broken.append(f"{target}#{frag}")
    check(f"all {len(links)} nav links resolve to a file and anchor", not broken)
    if broken:
        print("        broken:", broken[:8])
    if "toc.ncx" in names:
        ncx = ET.fromstring(z.read("OEBPS/toc.ncx"))
        orders = [int(p.get("playOrder")) for p in ncx.iter(f"{{{NCX}}}navPoint")]
        check(f"NCX playOrder unique across {len(orders)} navPoints",
              len(set(orders)) == len(orders))

    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print(f"PASSED — {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "dist/carmack-plan.epub"))
