# pandas 2.3.1

## read_csv

Use pandas.read_csv('data.csv'): it parses the file into a DataFrame, inferring dtypes per column. Common options are sep for the delimiter, header=None when there is no header row, and nrows to read only a sample.

```python
import pandas as pd

df = pd.read_csv("sales.csv", parse_dates=["date"])
print(df.dtypes)
print(df.head())
```

## What's new in 2.1.0: applymap deprecated

Since pandas 2.1.0, DataFrame.applymap is deprecated; use DataFrame.map(func) instead. DataFrame.map applies func elementwise to the frame, which is exactly what applymap did.

```python
# before
df.applymap(lambda x: round(x, 2))
# now
df.map(lambda x: round(x, 2))
```

## DataFrame.groupby

The signature is DataFrame.groupby(by=None, axis=0, level=None, as_index=True, sort=True, group_keys=True, observed=False, dropna=True). The by argument takes a column label, list of labels, or a function to group by.

## Merge

Combine frames with pd.merge(left, right, on='key', how='inner'). The how argument selects inner, left, right, or outer joins; use suffixes=('_l', '_r') when both frames share non-key column names.

## Plotting

DataFrames plot directly: df.plot(x='date', y='sales', kind='line'). The kind argument picks line, bar, hist, box, or scatter; pandas delegates to Matplotlib, so plt.show() displays the figure.
