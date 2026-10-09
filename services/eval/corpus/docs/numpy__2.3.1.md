# NumPy 2.3.1

## numpy.linspace

Call numpy.linspace(start, stop, num=50): it returns num evenly spaced samples over the closed interval [start, stop]. For example np.linspace(0, 1, 5) gives array([0.  , 0.25, 0.5 , 0.75, 1.  ]).

```python
import numpy as np

xs = np.linspace(0, 2 * np.pi, 100)
ys = np.sin(xs)
```

## NumPy 2.0 deprecations: np.trapz

NumPy 2.0 deprecated np.trapz in favor of np.trapezoid; the old name still works but emits a DeprecationWarning. Prefer numpy.trapezoid(y, x=None, dx=1.0) for trapezoidal integration going forward.

```python
import numpy as np

y = np.array([1, 2, 3])
area = np.trapezoid(y, dx=1.0)  # 4.0
```

## numpy.array

The signature is numpy.array(object, dtype=None, *, copy=True, order='K', subok=False, ndmin=0, like=None). NumPy 2.0 changed copy to keyword-only: copy=True always copies, copy=False raises if a copy is unavoidable, and copy=None copies only when needed.

## Broadcasting

Broadcasting describes how NumPy treats arrays with different shapes during arithmetic. Dimensions are compatible when they are equal or one of them is 1; a (3, 1) array and a (3, 4) array broadcast to (3, 4) without copying data.

## Random sampling

Use the Generator API: rng = np.random.default_rng(42); rng.normal(0, 1, size=1000). default_rng is the modern entry point — the legacy np.random.randn global functions are kept for compatibility but shouldn't be used in new code.
