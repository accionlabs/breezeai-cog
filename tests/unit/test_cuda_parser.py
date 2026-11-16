"""The CUDA parser: ``.cu`` / ``.cuh`` read with the CUDA grammar and the C-family extraction.

CUDA reads the C++ and C indexes after its own, so a ``.cu`` file's includes and calls resolve
into C and C++ files. Cross-file behaviour is tested end to end through ``analyze_repo`` at
``jobs`` 1 and 2, so the related indexes are shown to reach the workers.
"""

from __future__ import annotations

import gzip
import json

import pytest

from breezeai_cog import analyze_repo
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.cuda.parser import CudaParser
from breezeai_cog.schemas import FileRecord

KERNELS = """\
#include "scale.cuh"

__global__ void scale(float *x, float a) { x[threadIdx.x] *= a; }

__device__ __forceinline__ float square(float v) { return v * v; }

static void launch(float *x, cudaStream_t stream) {
    scale<<<1, 32, 0, stream>>>(x, 2.0f);
}
"""


def _parse(tmp_path, rel: str, text: str) -> FileRecord:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return CudaParser().parse_file(
        ParseContext(
            path=rel,
            abs_path=path,
            source=path.read_bytes(),
            repo_root=tmp_path,
            capture_statements=True,
        )
    )


def _repo(tmp_path, files: dict[str, str]):
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    return tmp_path


def _capture(repo, jobs: int) -> dict[str, dict]:
    result = analyze_repo(repo, out=repo.parent / f"out-{repo.name}-{jobs}", jobs=jobs)
    with gzip.open(result.out_path, "rt", encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh]
    return {r["path"]: r for r in records if r.get("__type") != "projectMetaData"}


def _calls(record: dict, function: str) -> dict[str, str | None]:
    fn = next(f for f in record["functions"] if f["name"] == function)
    return {c["name"]: c.get("path") for c in fn.get("calls") or []}


def test_kernels_and_device_functions_are_functions(tmp_path) -> None:
    rec = _parse(tmp_path, "k.cu", KERNELS)
    assert rec.language == "cuda"
    assert [f.name for f in rec.functions] == ["scale", "square", "launch"]
    launch = rec.functions[2]
    assert launch.isStatic is True
    # A kernel launch is a call to the kernel, resolved in the same file.
    assert [(c.name, c.path) for c in launch.calls] == [("scale", "k.cu")]


def test_a_cuda_header_holds_classes_and_device_functions(tmp_path) -> None:
    rec = _parse(
        tmp_path,
        "pool.cuh",
        "struct pool { int size; __device__ int next(int i) { return i + 1; } };\n"
        "template <typename T> __device__ T clamp01(T v) { return v < 0 ? 0 : v; }\n",
    )
    assert [c.name for c in rec.classes] == ["pool"]
    assert sorted(f.name for f in rec.functions) == ["clamp01", "next"]


@pytest.mark.parametrize("jobs", [1, 2])
def test_cuda_calls_and_includes_resolve_into_cuda_cpp_and_c(tmp_path, jobs) -> None:
    repo = _repo(
        tmp_path / "r",
        {
            "gpu/common.cuh": "__device__ float warp_sum(float v) { return v; }\n",
            "gpu/reduce.cu": (
                '#include "common.cuh"\n#include "host.hpp"\n#include "util.h"\n'
                "__global__ void reduce(float *x) { x[0] = warp_sum(x[0]); }\n"
                "void run(float *x) { reduce<<<1, 1>>>(x); log_size(1); checked(2); }\n"
            ),
            "host/host.hpp": "void log_size(int n);\n",
            "host/host.cpp": '#include "host.hpp"\nvoid log_size(int n) {}\n',
            "util/util.h": "int checked(int v);\n",
            "util/util.c": '#include "util.h"\nint checked(int v) { return v; }\n',
        },
    )
    records = _capture(repo, jobs)
    assert records["gpu/common.cuh"]["language"] == "cuda"
    reduce = records["gpu/reduce.cu"]
    assert reduce["importFiles"] == ["gpu/common.cuh", "host/host.hpp", "util/util.h"]
    assert _calls(reduce, "reduce") == {"warp_sum": "gpu/common.cuh"}
    assert _calls(reduce, "run") == {
        "reduce": "gpu/reduce.cu", "log_size": "host/host.cpp", "checked": "util/util.c",
    }


@pytest.mark.parametrize("jobs", [1, 2])
def test_a_cuda_include_does_not_decide_a_headers_language(tmp_path, jobs) -> None:
    # Only C and C++ files decide who owns a `.h`: a header included by C and by CUDA is C.
    repo = _repo(
        tmp_path / "r",
        {
            "api.h": "int area(int w, int h);\n",
            "api.c": '#include "api.h"\nint area(int w, int h) { return w * h; }\n',
            "k.cu": '#include "api.h"\nvoid run() { area(1, 2); }\n',
        },
    )
    records = _capture(repo, jobs)
    assert records["api.h"]["language"] == "c"
    assert _calls(records["k.cu"], "run") == {"area": "api.c"}
