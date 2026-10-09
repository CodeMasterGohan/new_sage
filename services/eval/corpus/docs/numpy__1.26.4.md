# NumPy 1.26.4

## numpy.reshape

Use arr.reshape(newshape): it returns a view on the same data whenever the new shape is compatible with the memory layout, so no copy is made. Use np.reshape(a, newshape) for the function form.

```python
import numpy as np

a = np.arange(12)
b = a.reshape(3, 4)   # view: b.base is a
c = a.reshape(3, 4).copy()  # explicit copy when you need one
```

## numpy.zeros

The signature is numpy.zeros(shape, dtype=float, order='C', *, like=None). It returns a new array of the given shape filled with zeros; shape may be an int or a tuple of ints.

## Data types

Every array has a dtype: np.arange(5).dtype is int64 on most platforms. Specify explicitly with dtype=np.float32 when memory or precision matters, and convert with arr.astype(np.int32).

## Indexing

Basic indexing uses slices: a[0:5] returns elements 0 through 4. Boolean indexing selects with a mask: a[a > 0] keeps the positive elements. Fancy indexing with an integer array picks arbitrary positions: a[[0, 2, 4]].
