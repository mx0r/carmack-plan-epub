# Fonts

These two files are vendored so the build does not depend on what happens to be
installed on the machine running it, and so CI can produce an identical book.

| File | |
| --- | --- |
| `FiraCode-Regular.ttf` | Fira Code, Regular, version 6.002 |
| `FiraCode-Bold.ttf` | Fira Code, Bold, version 6.002 |

Both are embedded in the EPUB and used for every monospaced element: dates, verbatim
changelogs, tables and code. Ligatures are disabled in the stylesheet — Carmack writes
`->` and `!=` a great deal, and turning those into arrows would misrepresent the text.

The body serif is not shipped here. The book requests Iowan Old Style, then Charter,
then Palatino, all of which Apple Books already provides.

## Licence

> Copyright 2014–2021 The Fira Code Project Authors
> (https://github.com/tonsky/FiraCode)
>
> This Font Software is licensed under the SIL Open Font License, Version 1.1.

The full licence text is in [`OFL.txt`](OFL.txt), copied verbatim from the
[Fira Code repository](https://github.com/tonsky/FiraCode/blob/master/LICENSE).

The OFL permits redistribution, including bundling inside a document, provided the
copyright notice and the licence travel with the files. The built EPUB embeds both
fonts, so `build_epub.py` reads `OFL.txt` at build time and renders it as a "Font
licence" page in the back matter — the licence ships inside the book as well as beside
it here. If the file is missing the build still succeeds, but it warns and omits that
page, which would leave the EPUB out of compliance.
