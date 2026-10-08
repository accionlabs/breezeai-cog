"""Wall-clock parse cost vs file size, per parser (parser review guide §6.1, "Measure it").

Parses the synthetic samples from ``tests/unit/test_scaling.py`` at doubling sizes and prints
the time per method/route. Linear code keeps that figure flat as the file grows; a per-item
scan makes it climb. The unit test guards the same property deterministically (line counts);
use this for real timings — before and after a parser change, or on a new sample.

    uv run python scripts/bench_parse_scaling.py                     # all samples
    uv run python scripts/bench_parse_scaling.py java-vertx --max 8000
"""

from __future__ import annotations

import argparse
import gc
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "unit"))  # the samples live with the unit test

from breezeai_cog.core import registry  # noqa: E402
from breezeai_cog.parsers.base import ParseContext  # noqa: E402
from test_scaling import SAMPLES  # noqa: E402


def _parse_seconds(rel: str, source: bytes) -> float:
    parser = registry.select(rel, source)
    ctx = ParseContext(path=rel, abs_path=Path(rel), source=source, repo_root=Path("."),
                       capture_statements=True)
    gc.collect()  # don't bill this run for garbage left by the previous one
    start = time.perf_counter()
    parser.parse_file(ctx)
    return time.perf_counter() - start


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("samples", nargs="*", metavar="SAMPLE",
                    help=f"which samples (default: all of {', '.join(sorted(SAMPLES))})")
    ap.add_argument("--min", type=int, default=500, help="smallest size (methods/routes)")
    ap.add_argument("--max", type=int, default=4000, help="largest size (methods/routes)")
    args = ap.parse_args()
    unknown = sorted(set(args.samples) - set(SAMPLES))
    if unknown:
        known = ", ".join(sorted(SAMPLES))
        ap.error(f"unknown sample(s) {', '.join(unknown)}; choose from {known}")

    registry.discover_builtin()
    for name in args.samples or sorted(SAMPLES):
        rel, generate = SAMPLES[name]
        _parse_seconds(rel, generate(5).encode())  # warm one-time caches
        print(name)
        n = args.min
        while n <= args.max:
            source = generate(n).encode()
            seconds = _parse_seconds(rel, source)
            lines = source.count(b"\n")
            print(f"  n={n:6d}  lines={lines:7d}  {seconds * 1000:10.1f} ms"
                  f"  {seconds / n * 1e6:8.1f} µs per item")
            n *= 2


if __name__ == "__main__":
    main()
