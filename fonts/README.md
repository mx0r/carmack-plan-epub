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

Taken from the fonts' own `name` table (IDs 0 and 13). The OFL permits redistribution,
including bundling inside a document, provided the licence travels with the files.

**Still to add:** the full `OFL.txt`, copied from the
[Fira Code repository](https://github.com/tonsky/FiraCode/blob/master/LICENSE), should
sit in this directory. It was not committed because the machine that assembled this
repository was offline; the notice above is transcribed from the font binaries rather
than from the canonical licence file.
