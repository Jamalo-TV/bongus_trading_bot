import pytest
import timeit
import numpy as np
try:
    from core_c.fast_math import compute_ewma
except ImportError:
    # This might happen if not compiled yet
    compute_ewma = None

def python_compute_ewma(data, alpha):
    result = np.empty_like(data)
    result[0] = data[0]
    for i in range(1, len(data)):
        result[i] = alpha * data[i] + (1 - alpha) * result[i-1]
    return result

@pytest.mark.skipif(compute_ewma is None, reason="Cython module not compiled")
def test_math_parity():
    data = np.random.rand(1000).astype(np.float64)
    alpha = 0.1
    
    assert compute_ewma is not None
    expected = python_compute_ewma(data, alpha)
    actual = compute_ewma(data, alpha)
    
    assert np.allclose(expected, actual)

@pytest.mark.skipif(compute_ewma is None, reason="Cython module not compiled")
def test_performance_boost():
    size = 10000
    data = np.random.rand(size).astype(np.float64)
    alpha = 0.1
    
    assert compute_ewma is not None
    # We use a smaller size because Python loop is very slow
    py_time = timeit.timeit(lambda: python_compute_ewma(data, alpha), number=10)
    cy_time = timeit.timeit(lambda: compute_ewma(data, alpha), number=10) # type: ignore
    
    print(f"Python time: {py_time:.4f}s")
    print(f"Cython time: {cy_time:.4f}s")
    
    # Cython should be significantly faster.
    assert cy_time * 2 < py_time
