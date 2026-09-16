"""Independent numeric review conditions beyond a command's success message."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
import numpy as np
from vecdiff.knn import l2_normalize, topk_cosine


def main() -> int:
    normalized = l2_normalize(np.array([[3, 4], [0, 0]], dtype=np.float32))
    if normalized.dtype != np.float32 or not np.isfinite(normalized).all():
        raise AssertionError("Normalization must preserve float32 and finite zero rows")
    if not np.allclose(normalized, [[0.6, 0.8], [0, 0]], atol=1e-6):
        raise AssertionError("Normalization must produce unit vectors")
    indices, scores = topk_cosine(np.array([[1, 0], [0.8, 0.6], [0, 1]], dtype=np.float32), [0], 2)
    if indices.tolist() != [[1, 2]] or scores.dtype != np.float32:
        raise AssertionError("Neighbors must exclude self and use descending similarity order")
    if not np.allclose(scores, [[0.8, 0]], atol=1e-6):
        raise AssertionError("Cosine scores changed")
    print("Numeric contracts: PASS (normalization, zero rows, dtype, ordering, self-exclusion)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
