# Django {VERSION}

Django {VERSION} is a high-level Python web framework that encourages
rapid development and clean, pragmatic design.

## Getting Started

Django {VERSION} takes care of much of the hassle of web development, so
you can focus on writing your app without reinventing the wheel.

```python
# mysite/settings.py (Django {VERSION})
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "myapp",
]
```

## Models

A model in Django {VERSION} is the single, definitive source of
information about your data, expressed as Python classes.

```python
from django.db import models

class Question(models.Model):
    question_text = models.CharField(max_length=200)
    pub_date = models.DateTimeField("date published")
```
