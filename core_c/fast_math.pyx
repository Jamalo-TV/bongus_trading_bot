# cython: language_level=3
import numpy as np
cimport numpy as cnp
cimport cython
from libc.math cimport fabs

@cython.boundscheck(False)
@cython.wraparound(False)
def compute_ewma(double[:] data, double alpha):
    cdef int n = data.shape[0]
    cdef double[:] result = np.empty(n, dtype=np.float64)
    cdef int i

    result[0] = data[0]
    with nogil:
        for i in range(1, n):
            result[i] = alpha * data[i] + (1 - alpha) * result[i-1]
    
    return np.asarray(result)


