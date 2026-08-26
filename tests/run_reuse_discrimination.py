#!/usr/bin/env python3
"""
Reuse-discrimination test: does the saved "use pdftotext" rule fire on the RIGHT
task and NOT on a similar-but-different task?

Flow:
  1. POST /reset (fresh).
  2. Run a PDF task -> "just ask" should save "use pdftotext for PDF text".
  3. POST /save (archive, so the strategy is linkable/retrievable).
  4. Positive: a NEW PDF task  -> should REUSE pdftotext.
  5. Negative: a .docx task     -> should NOT use pdftotext (needs unzip/python-docx).

Reports, per step, whether the task was solved and whether the tool trace used
"pdftotext" (from the bash command args).
"""

import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from capability_harness import (  # noqa: E402
    make_env_pdf_text, make_env_docx_text, run_task, check_oracle,
)

SCRATCH = os.path.expanduser("~/mneme_hct")


def _post(base, path):
    req = urllib.request.Request(base.rstrip("/") + path, data=b"{}",
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def _materialize(env, tag):
    d = os.path.join(SCRATCH, tag, "data")
    os.makedirs(d, exist_ok=True)
    for name, content in env.files.items():
        mode = "wb" if isinstance(content, (bytes, bytearray)) else "w"
        with open(os.path.join(d, name), mode) as f:
            f.write(content)
    return lambda task: task.replace("data/", d + "/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8082")
    args = ap.parse_args()

    pdf = make_env_pdf_text(seed=9)
    docx = make_env_docx_text(seed=13)

    _post(args.base, "/reset")

    # 1. learn: PDF task1 (the "just ask" should save "use pdftotext")
    abs1 = _materialize(pdf, "disc_pdf_learn")
    rr = run_task(args.base, abs1(pdf.task1))
    print(f"[learn  pdf] solved={check_oracle(rr.answer, pdf.oracle1)}  "
          f"pdftotext_used={rr.used('pdftotext')}  tools={rr.tools}  {rr.elapsed:.1f}s")

    # 2. archive so the strategy is retrievable
    try:
        print(f"[archive] {_post(args.base, '/save')}")
    except Exception as e:
        print(f"[archive] error: {e}")

    # 3. positive: a NEW PDF task -> should reuse pdftotext
    abs2 = _materialize(pdf, "disc_pdf_pos")
    rr_pos = run_task(args.base, abs2(pdf.task2))
    print(f"[positive pdf] solved={check_oracle(rr_pos.answer, pdf.oracle2)}  "
          f"pdftotext_used={rr_pos.used('pdftotext')}  tools={rr_pos.tools}  {rr_pos.elapsed:.1f}s")
    print(f"    answer: {rr_pos.answer.strip()[:120]!r}")

    # 4. negative: a .docx task -> should NOT use pdftotext
    abs3 = _materialize(docx, "disc_docx_neg")
    rr_neg = run_task(args.base, abs3(docx.task1))
    print(f"[negative docx] solved={check_oracle(rr_neg.answer, docx.oracle1)}  "
          f"pdftotext_used={rr_neg.used('pdftotext')}  tools={rr_neg.tools}  {rr_neg.elapsed:.1f}s")
    print(f"    answer: {rr_neg.answer.strip()[:120]!r}")
    print(f"    commands: {[c[:60] for c in rr_neg.commands]}")

    print()
    print("VERDICT: positive should be solved + pdftotext_used=True;")
    print("         negative should be solved + pdftotext_used=False.")


if __name__ == "__main__":
    main()
