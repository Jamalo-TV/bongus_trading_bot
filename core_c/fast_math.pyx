# cython: language_level=3
import numpy as np
cimport numpy as cnp
from libc.math cimport fabs

def compute_mean_abs_error(double[:] y_true, double[:] y_pred):
    cdef int n = y_true.shape[0]
    cdef double total_error = 0.0
    cdef int i

    with nogil:
        for i in range(n):
            total_error += fabs(y_true[i] - y_pred[i])
    
    return total_error / n
