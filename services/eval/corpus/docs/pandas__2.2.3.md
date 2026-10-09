# pandas 2.2.3

## DataFrame.loc

Use df.loc[label] for label-based selection: df.loc['a'] returns the row labeled 'a', and df.loc['a':'c', 'x':'y'] slices rows and columns inclusively by label.

```python
import pandas as pd

df = pd.DataFrame({"x": [1, 2, 3], "y": [4, 5, 6]}, index=["a", "b", "c"])
row = df.loc["b"]            # Series for row 'b'
sub = df.loc["a":"b", ["x"]] # inclusive slice
```

## Series.value_counts

The signature is Series.value_counts(normalize=False, sort=True, ascending=False, bins=None, dropna=True). It returns a Series with unique values as the index and their counts as values.

## apply

DataFrame.apply(func, axis=0) applies func along an axis: axis=0 maps over columns, axis=1 over rows. Prefer vectorized operations or DataFrame.map for elementwise work — apply is a Python-level loop.

## Missing data

Detect gaps with df.isna() and fill them with df.fillna(0); drop incomplete rows with df.dropna(). The nullable dtypes (Int64, boolean, string) represent missing values as pd.NA instead of NaN.
