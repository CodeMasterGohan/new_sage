# PyTorch 2.8.0

## torch.compile

Wrap the model once: compiled_model = torch.compile(model). torch.compile was introduced in PyTorch 2.0 and speeds up the model with no code changes; call the compiled model exactly like the original.

```python
import torch

model = MyModel().eval()
compiled = torch.compile(model)

with torch.no_grad():
    out = compiled(torch.randn(1, 3, 224, 224))
```

## Compiling models in PyTorch 2.x

PyTorch 2.0 introduced torch.compile, a full-graph compiler for eager models: torch.compile(model) returns an optimized callable with identical semantics. Later 2.x releases widened backend and dynamic-shape support.

## torch.nn.Linear

The signature is torch.nn.Linear(in_features, out_features, bias=True, device=None, dtype=None). It applies the affine map y = xA^T + b, where A has shape (out_features, in_features).

## Autograd

Tensors with requires_grad=True record every operation into a computation graph. Call loss.backward() to populate .grad on the leaf tensors, then optimizer.step() to update parameters; wrap inference in torch.no_grad() to skip graph construction.

## DataLoader

torch.utils.data.DataLoader(dataset, batch_size=32, shuffle=True, num_workers=4) batches and parallelizes loading. Set pin_memory=True when training on CUDA so host-to-device copies go through page-locked memory.
