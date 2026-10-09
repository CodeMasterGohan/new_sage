# Django 4.2.24

## Field choices

Pass a choices iterable of (value, label) pairs to the field: status = models.CharField(max_length=2, choices=[('DR', 'Draft'), ('PB', 'Published')]). Django validates assigned values and provides get_status_display().

```python
from django.db import models

class Article(models.Model):
    DRAFT = "DR"
    PUBLISHED = "PB"
    STATUS_CHOICES = [
        (DRAFT, "Draft"),
        (PUBLISHED, "Published"),
    ]
    status = models.CharField(max_length=2, choices=STATUS_CHOICES, default=DRAFT)

    def __str__(self):
        return self.get_status_display()
```

## path()

The signature is path(route, view, kwargs=None, name=None). The route is a string with angle-bracket converters like <int:year>, and view is a view function or the result of include().

```python
from django.urls import include, path

urlpatterns = [
    path("articles/<int:year>/", views.year_archive),
    path("blog/", include("blog.urls")),
]
```

## QuerySet.get

get(**kwargs) returns the single object matching the query, raising Article.DoesNotExist when nothing matches and MultipleObjectsReturned when more than one matches. Use it for unique lookups like primary keys.

## Template tags

Load custom tag libraries with {% load my_tags %} at the top of the template. Built-in tags like {% for %}, {% if %}, and {% block %} cover iteration, conditionals, and template inheritance.
