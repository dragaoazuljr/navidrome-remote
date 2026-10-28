"""_digest.py — agrega o espiao NDJSON em markdown (chamado por c1_probe.sh)."""
import json
import sys
from collections import Counter


def main(path):
    cnt = Counter()
    samples = {}
    for line in open(path, errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            cnt["<parse-error>"] += 1
            continue
        key = obj.get("event", "<none>")
        cnt[key] += 1
        samples.setdefault(key, obj)
    print("| evento | n | exemplo (truncado) |")
    print("|---|---|---|")
    for key, num in sorted(cnt.items()):
        ex = json.dumps(samples[key], ensure_ascii=False)
        tail = "..." if len(ex) > 150 else ""
        print(f"| `{key}` | {num} | `{ex[:150]}`{tail} |")


if __name__ == "__main__":
    main(sys.argv[1])
