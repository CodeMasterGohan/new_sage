# PyTorch 2.7.1

## Tensor.to

Call tensor.to('cuda'): it returns a copy of the tensor on the GPU. Use tensor.to('cuda:0') to pick a specific device, and model.to('cuda') moves every parameter of a module.

```python
import torch

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
x = torch.randn(3, 3).to(device)
model = MyModel().to(device)
```

## torch.tensor

The signature is torch.tensor(data, *, dtype=None, device=None, requires_grad=False, pin_memory=False). The data argument is a list, tuple, or NumPy array; everything after data is keyword-only.

## nn.Module

Subclass torch.nn.Module and define forward(): parameters registered as attributes (self.fc = nn.Linear(...)) are tracked automatically. Call model.train() before training and model.eval() before inference to switch dropout/batchnorm behavior.

## Optimizers

torch.optim.Adam(model.parameters(), lr=1e-3) is the default choice for most training. The loop is zero_grad(), loss.backward(), optimizer.step(); schedulers like CosineAnnealingLR adjust the learning rate per epoch.
