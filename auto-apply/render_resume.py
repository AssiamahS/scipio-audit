#!/usr/bin/env python3
"""resume.md / resume-infra.md -> ATS-safe HTML.

One source of truth per variant. The markdown is the master; the HTML is
generated, never hand-edited. Keeps the two from drifting apart, which is how
the em dashes and the two-page overflow survived the last cleanup.

Optional --headline mirrors the exact job title being applied to into the line
under the name. Only pass a title Sylvester has honestly done.
"""
import argparse
import html
import pathlib
import re

# no ligatures: "ﬁ" extracts as one codepoint and kills keyword matching
BODY_STYLE = (
    "font-family: Calibri, Helvetica, sans-serif; font-size: 9.2pt; "
    "line-height: 1.1; font-variant-ligatures: none; "
    "-webkit-font-variant-ligatures: none;"
)
H1 = 'style="font-size: 14pt; margin: 0 0 1pt 0;"'
H2 = 'style="font-size: 10.2pt; margin: 5pt 0 1pt 0;"'
P_SMALL = 'style="margin: 0; font-size: 8.9pt;"'
UL = 'style="margin: 1pt 0 3pt 0; padding-left: 14pt;"'
ROLE_P = 'style="margin: 3pt 0 1pt 0;"'


def inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<i>\1</i>', text)
    return text


COMPACT_BODY_STYLE = BODY_STYLE.replace("line-height: 1.1", "line-height: 1.03")
COMPACT_H2 = H2.replace("margin: 5pt 0 1pt 0", "margin: 3pt 0 1pt 0")
COMPACT_UL = UL.replace("margin: 1pt 0 3pt 0", "margin: 0 0 2pt 0")


def render(md: str, headline: str | None = None, compact: bool = False) -> str:
    """compact=True squeezes leading/margins (not font size) so a tailored
    variant whose JD-echo wordings run a few words long still lands on ONE
    page (8/26: 709-word tailored md spilled to page 2, rules blocked it)."""
    global BODY_STYLE, H2, UL
    saved = (BODY_STYLE, H2, UL)
    if compact:
        BODY_STYLE, H2, UL = COMPACT_BODY_STYLE, COMPACT_H2, COMPACT_UL
    try:
        return _render(md, headline)
    finally:
        BODY_STYLE, H2, UL = saved


def _render(md: str, headline: str | None = None) -> str:
    lines = md.splitlines()
    out, in_list = [], False

    def close_list():
        nonlocal in_list
        if in_list:
            out.append('</ul>')
            in_list = False

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith('# '):
            close_list()
            out.append(f'<h1 {H1}>{inline(line[2:])}</h1>')
            if headline:
                out.append(f'<p {P_SMALL}><b>{inline(headline)}</b></p>')
        elif line.startswith('## '):
            close_list()
            out.append(f'<h2 {H2}>{inline(line[3:])}</h2>')
        elif line.startswith('### '):
            close_list()
            out.append(f'<p {ROLE_P}><b>{inline(line[4:])}</b></p>')
        elif line.startswith('- '):
            if not in_list:
                out.append(f'<ul {UL}>')
                in_list = True
            out.append(f'<li>{inline(line[2:])}</li>')
        else:
            close_list()
            # consecutive plain lines (skills rows, education pairs) share one
            # <p> with <br> so each row doesn't cost a paragraph margin
            if out and out[-1].startswith(f'<p {P_SMALL}>') and out[-1].endswith('</p>'):
                out[-1] = out[-1][:-4] + '<br>' + inline(line) + '</p>'
            else:
                out.append(f'<p {P_SMALL}>{inline(line)}</p>')
    close_list()
    body = '\n'.join(out)
    return (f'<html>\n<head><meta charset="utf-8"></head>\n'
            f'<body style="{BODY_STYLE}">\n{body}\n</body>\n</html>\n')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('source')
    ap.add_argument('output')
    ap.add_argument('--headline', help='exact job title to mirror in the header')
    ap.add_argument('--compact', action='store_true', help='tighter leading to hold one page')
    a = ap.parse_args()
    md = pathlib.Path(a.source).read_text()
    pathlib.Path(a.output).write_text(render(md, a.headline, a.compact))
    print(f'rendered {a.output}')


if __name__ == '__main__':
    main()
