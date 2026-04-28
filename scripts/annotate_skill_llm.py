"""Run the annotate-LLM pass on a v1 skill JSON. Writes a `.v2.json`
sidecar next to the original.

Usage:
    .venv/bin/python scripts/annotate_skill_llm.py \\
        --skill skills/curate_layout.json \\
        --portal sample_portal \\
        --client groq
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pilot.agent.ai_client import get_client
from pilot.agent.annotate_llm import annotate_skill, write_v2_sidecar


async def amain(args: argparse.Namespace) -> int:
    skill_path = Path(args.skill).resolve()
    if not skill_path.exists():
        print(f"FATAL: skill not found: {skill_path}", file=sys.stderr)
        return 2
    v1 = json.loads(skill_path.read_text(encoding="utf-8"))

    client = get_client(args.client)
    try:
        meta = await annotate_skill(
            client=client,
            v1_skill=v1,
            portal_id=args.portal,
            model=args.model,
        )
    finally:
        await client.close()

    sidecar = write_v2_sidecar(skill_path, meta)
    print(f"wrote {sidecar}")
    print(json.dumps(meta, indent=2)[:1500])
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--skill", required=True)
    p.add_argument("--portal", default=None)
    p.add_argument("--client", default="groq")
    p.add_argument("--model", default=None)
    args = p.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
