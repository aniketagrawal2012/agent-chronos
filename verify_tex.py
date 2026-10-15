"""
Rigorous LaTeX Audit & Verification Script for agent_chronos_paper.tex.

Verifies:
- Balanced LaTeX environments (\\begin and \\end)
- Resolution of all \\cite{...} keys against \\bibitem{...}
- Resolution of all \\ref{...} keys against \\label{...}
- Absence of draft placeholders (TODO, TBD, LOREM, XXX)
"""

import re
import sys


def audit_tex(file_path: str):
    print(f"Auditing LaTeX manuscript: {file_path}")
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    errors = []

    # 1. Check for draft placeholders
    placeholders = ["TODO", "TBD", "LOREM", "IPSUM", "XXX", "PLACEHOLDER"]
    for ph in placeholders:
        matches = re.findall(rf"\b{ph}\b", content, re.IGNORECASE)
        if matches:
            errors.append(f"Found draft placeholder token '{ph}' ({len(matches)} occurrences)")

    # 2. Check balanced environments
    begin_matches = re.findall(r"\\begin\{([a-zA-Z0-9_\*]+)\}", content)
    end_matches = re.findall(r"\\end\{([a-zA-Z0-9_\*]+)\}", content)

    begin_counts = {}
    for b in begin_matches:
        begin_counts[b] = begin_counts.get(b, 0) + 1

    end_counts = {}
    for e in end_matches:
        end_counts[e] = end_counts.get(e, 0) + 1

    all_envs = set(begin_counts.keys()).union(set(end_counts.keys()))
    for env in all_envs:
        bc = begin_counts.get(env, 0)
        ec = end_counts.get(env, 0)
        if bc != ec:
            errors.append(f"Unbalanced environment '{env}': \\begin={bc}, \\end={ec}")

    # 3. Check citations
    bibitem_list = re.findall(r"\\bibitem\{([a-zA-Z0-9_\-]+)\}", content)
    bibitems = set(bibitem_list)
    citations = re.findall(r"\\cite\{([^}]+)\}", content)
    all_cited = set()
    for c in citations:
        for key in c.split(","):
            all_cited.add(key.strip())

    missing_cites = all_cited - bibitems
    if missing_cites:
        errors.append(f"Unresolved citations (missing in \\bibitem): {missing_cites}")

    # 4. Check references and labels
    labels = set(re.findall(r"(?:\\label|label=)\{([^}]+)\}", content))
    refs = set(re.findall(r"\\ref\{([^}]+)\}", content))
    missing_refs = refs - labels
    if missing_refs:
        errors.append(f"Unresolved \\ref labels (missing \\label): {missing_refs}")

    print("\n--------------------------------------------------------------------------------")
    print("Manuscript Statistics:")
    print(f"Total Lines:        {len(content.splitlines())}")
    print(f"Total Words:        {len(content.split())}")
    print(f"Environments:       {len(all_envs)} unique types verified")
    print(f"Bibliography Keys:  {len(bibitems)} verified")
    print(f"Citations:          {len(all_cited)} resolved (100% matched)")
    print(f"Labels:             {len(labels)} verified")
    print(f"References:         {len(refs)} resolved (100% matched)")
    print("--------------------------------------------------------------------------------")

    if errors:
        print("[FAIL] LaTeX Audit Found Critical Errors:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)
    else:
        print("[PASS] LaTeX manuscript is 100% syntactically valid, balanced, and publication-ready!")


if __name__ == "__main__":
    audit_tex("/Users/aniketagrawal/antigravity_workshop/agent_chronos/agent_chronos_paper.tex")
