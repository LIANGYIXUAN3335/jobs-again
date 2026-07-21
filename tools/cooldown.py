"""Add / list per-company application cooldowns in config.yaml.
  python tools/cooldown.py plaid 60 "Ashby 3 apps / 60 days — cap reached"
  python tools/cooldown.py --list
While a cooldown is active the scanner skips the company and the UI hides its roles.
"""
import datetime
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config.yaml"


def main(argv):
    text = CFG.read_text(encoding="utf-8")
    cfg = yaml.safe_load(text) or {}
    cds = cfg.get("cooldowns") or {}

    if not argv or argv[0] in ("--list", "-l"):
        today = datetime.date.today().isoformat()
        if not cds:
            print("no cooldowns set")
            return
        for name, cd in cds.items():
            state = "ACTIVE" if str(cd.get("until", "")) >= today else "expired"
            print(f"  {name:14} until {cd.get('until')} [{state}] — {cd.get('reason', '')}")
        return

    if len(argv) < 2:
        print(__doc__)
        sys.exit(1)
    company = argv[0].strip().lower().replace(" ", "")
    days = int(argv[1])
    reason = argv[2] if len(argv) > 2 else f"{days}-day application cooldown"
    until = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()

    # Append/update the entry as a one-line flow-style mapping so the file stays readable.
    line = f'  {company}: {{ until: {until}, reason: "{reason}" }}\n'
    if "cooldowns:" not in text:
        text = text.rstrip() + "\n\ncooldowns:\n" + line
    else:
        lines, done, out = text.splitlines(keepends=True), False, []
        in_block = False
        for ln in lines:
            if ln.startswith("cooldowns:"):
                in_block = True
                out.append(ln)
                continue
            if in_block and ln.strip().startswith(f"{company}:"):
                out.append(line); done = True; in_block = False; continue
            if in_block and (not ln.startswith(("  ", "\t")) and ln.strip()):
                if not done:
                    out.append(line); done = True
                in_block = False
            out.append(ln)
        if in_block and not done:
            out.append(line)
        text = "".join(out)
    CFG.write_text(text, encoding="utf-8")
    print(f"✓ {company} on cooldown until {until} — {reason}")
    print("  (scanner will skip it; existing roles are hidden from review. Run tools/rescore.py not needed.)")


if __name__ == "__main__":
    main(sys.argv[1:])
