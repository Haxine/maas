# Copyright 2014 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

""":class:`EventType` and friends."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = [
    'EventType',
    ]


from django.db.models import (
    CharField,
    IntegerField,
    )
from maasserver import DefaultMeta
from maasserver.models.cleansave import CleanSave
from maasserver.models.timestampedmodel import TimestampedModel


class EventType(CleanSave, TimestampedModel):
    """A type for events.

    :ivar name: The event type's identifier.
    :ivar name: A human-readable description of the event type.
    :ivar level: Severity of the event.  These match the standard
        Python log levels; higher values are more significant.
    """

    name = CharField(
        max_length=255, unique=True, blank=False, editable=False)

    description = CharField(max_length=255, blank=False, editable=False)

    level = IntegerField(blank=False, editable=False)

    class Meta(DefaultMeta):
        verbose_name = "Event type"

    def __unicode__(self):
        return "%s (level=%s, description=%s)" % (
            self.name, self.level, self.description)
