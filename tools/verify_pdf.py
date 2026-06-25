"""What an ATS actually sees. Verifies the rendered PDFs, not the YAML that produced them.

`resume_score` scores the merged data structure. Typst then renders that structure to a PDF, and
an applicant tracking system reads neither — it reads the PDF's embedded TEXT LAYER. Those three
can disagree: a layout can render beautifully and extract as `(cid:34)` soup, put the email
behind an icon glyph so it vanishes from the extraction, or interleave lines so the reading order
scrambles. Every one of those is invisible in the YAML and invisible on screen.

So this opens the actual file and checks what a parser gets:
  - text extracts at all, with no `(cid:NN)` runs or U+FFFD replacement characters
  - phone, email, location and LinkedIn survive as LITERAL text (an address carried only by a
    hyperlink target is not there as far as a parser is concerned)
  - every employer name and every date range from the locked base résumé is present
  - section headings appear in the template's intended order, not interleaved
  - page count is within bounds

The Typst template is single-column and low-risk, which is exactly why this had never been
checked — 1400 PDFs went out on the assumption. This settles it in a few seconds.

Needs `pdftotext` (poppler: brew install poppler). Degrades to a clear message if absent.

    python tools/verify_pdf.py --sample 40      # spot-check
    python tools/verify_pdf.py --all            # every PDF in output/resumes
    python tools/verify_pdf.py <file.pdf> ...
"""
from __future__ import annotations

import argparse
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as cfgmod  # noqa: E402
from app import tailoring  # noqa: E402

SECTION_ORDER = ["SUMMARY", "SKILLS", "PROFESSIONAL EXPERIENCE", "EDUCATION"]
MAX_PAGES = 2
_CID = re.compile(r"\(cid:\d+\)")
_DIGITS = re.compile(r"\D+")


def _extract(pdf: Path) -> str:
    out = subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "pdftotext failed")
    # pdftotext marks a page break with a form feed and glues it to the first line of the next
    # page, so a heading that happens to start page 2 reads as "\x0cEDUCATION". That's the
    # extractor's page separator, not a defect — turn it into a newline before anything else
    # looks at the text. (147 of 1433 résumés page-break exactly there.)
    return out.stdout.replace("\x0c", "\n")


def _pages(pdf: Path) -> int | None:
    if not shutil.which("pdfinfo"):
        return None
    out = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True)
    m = re.search(r"^Pages:\s+(\d+)", out.stdout, re.M)
    return int(m.group(1)) if m else None


def check(pdf: Path, base: dict) -> list[str]:
    """Problems found, in the order an ATS would trip over them. Empty list = clean."""
    problems: list[str] = []
    try:
        text = _extract(pdf)
    except Exception as e:
        return [f"no text layer: {e}"]

    if len(text.strip()) < 400:
        problems.append(f"text layer nearly empty ({len(text.strip())} chars) — likely an image")
    if _CID.search(text):
        problems.append("(cid:NN) glyph codes in the extraction — a font isn't mapping to Unicode")
    if "�" in text:
        problems.append("U+FFFD replacement characters in the extraction")

    flat = re.sub(r"\s+", " ", text)
    contact = base.get("contact") or {}
    if contact.get("email") and contact["email"].lower() not in flat.lower():
        problems.append(f"email missing from the text layer ({contact['email']})")
    if contact.get("phone"):
        # Compare digits only: the rendering may hyphenate or space the number differently.
        want = _DIGITS.sub("", contact["phone"])
        if want and want not in _DIGITS.sub("", flat):
            problems.append(f"phone digits missing from the text layer ({contact['phone']})")
    if contact.get("linkedin") and contact["linkedin"].lower() not in flat.lower():
        problems.append("LinkedIn URL present only as a link target, not as readable text")
    if base.get("name") and base["name"].lower() not in flat.lower():
        problems.append("candidate name missing from the text layer")

    for job in base.get("experience", []):
        if job["company"][:24].lower() not in flat.lower():
            problems.append(f"employer missing: {job['company'][:34]}")
        years = re.findall(r"\d{4}", job.get("dates", ""))
        for y in years:
            if y not in flat:
                problems.append(f"date {y} ({job['company'][:18]}) missing from the text layer")
                break
    # The template prints the EDUCATION heading unconditionally, so a heading check alone passes
    # on an empty section. Look for the schools themselves.
    for edu in base.get("education", []):
        if edu["school"][:24].lower() not in flat.lower():
            problems.append(f"school missing: {edu['school'][:34]}")

    # Headings must be matched as WHOLE LINES. A substring search finds "EDUCATION" inside the
    # Haoxiong bullet's "educational modules" and concludes the reading order is scrambled.
    upper = text.upper()
    pos = {}
    for s in SECTION_ORDER:
        m = re.search(rf"^[ \t]*{re.escape(s)}[ \t]*$", upper, re.M)
        if m:
            pos[s] = m.start()
    found = [(p, s) for s, p in pos.items()]
    found.sort(key=lambda t: SECTION_ORDER.index(t[1]))
    missing = [s for s in SECTION_ORDER if s not in pos and s != "SUMMARY"]
    if missing:
        problems.append("section heading(s) not extracted: " + ", ".join(missing))
    if found != sorted(found):
        problems.append("section headings extract out of order — reading order is scrambled")

    n = _pages(pdf)
    if n is not None and n > MAX_PAGES:
        problems.append(f"{n} pages (limit {MAX_PAGES})")
    return problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--sample", type=int, default=25)
    ap.add_argument("--seed", type=int, default=0, help="sample seed, for a repeatable spot-check")
    args = ap.parse_args(argv)

    if not shutil.which("pdftotext"):
        print("pdftotext not found — install poppler (brew install poppler). "
              "Without it there is no way to see what a parser sees.")
        return 2

    cfg = cfgmod.load_config()
    base = tailoring.load_base(cfg)
    if args.files:
        pdfs = list(args.files)
        total = len(pdfs)
    else:
        rdir = cfgmod.resolve_path(cfg["output_dirs"]["resumes"])
        pdfs = sorted(rdir.glob("*.pdf"))
        total = len(pdfs)
        if not args.all and total > args.sample:
            random.Random(args.seed).shuffle(pdfs)
            pdfs = sorted(pdfs[:args.sample])
    if not pdfs:
        print("no PDFs found")
        return 1

    bad = 0
    for p in pdfs:
        problems = check(p, base)
        if problems:
            bad += 1
            print(f"\n✗ {p.name}")
            for x in problems:
                print(f"    {x}")
    scope = "every PDF" if len(pdfs) == total else f"random sample of {total}, seed {args.seed}"
    print(f"\n{len(pdfs) - bad}/{len(pdfs)} clean ({scope})")
    if bad:
        print("A failure here is a TEMPLATE problem, not a per-résumé one — fix "
              "templates/resume.typ and re-ingest, don't patch the YAML.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
