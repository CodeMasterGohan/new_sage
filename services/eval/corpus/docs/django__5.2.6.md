# Django 5.2.6

## GeneratedField

Declare a GeneratedField with a database expression: square = models.GeneratedField(expression=F('side') ** 2, output_field=models.IntegerField(), db_persist=True). Django 5.0 added GeneratedField for columns the database computes on every write.

```python
from django.db import models
from django.db.models import F

class Square(models.Model):
    side = models.IntegerField()
    area = models.GeneratedField(
        expression=F("side") ** 2,
        output_field=models.IntegerField(),
        db_persist=True,
    )
```

## What's new in Django 5.0: computed model fields

Django 5.0 added GeneratedField, a model field whose value the database computes from an expression. Set db_persist=True to store the computed value in the table, or db_persist=False for a virtual column evaluated on read.

## QuerySet.filter

The signature is filter(*args, **kwargs). It returns a new QuerySet containing objects matching the given lookup parameters, for example Entry.objects.filter(headline__startswith='Django').

## Migrations

Migrations propagate model changes to the database schema. Run python manage.py makemigrations to generate a migration from model edits, then python manage.py migrate to apply it. Never edit applied migrations by hand on a shared database.

## Admin site

Register models to get a free admin interface: admin.site.register(Article). Customize with ModelAdmin subclasses — list_display controls the changelist columns, list_filter adds sidebar filters, and search_fields enables the search box.
