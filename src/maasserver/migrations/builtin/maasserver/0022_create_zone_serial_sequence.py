# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations

# At the time of writing this should match the definition in
# maasserver.dns.config.zone_serial, and vice-versa.
sequence_create = ("""\
DO
$$
BEGIN
    CREATE SEQUENCE maasserver_zone_serial_seq
    MINVALUE {minvalue:d} MAXVALUE {maxvalue:d};
EXCEPTION WHEN duplicate_table THEN
    -- Do nothing, it already exists.
END
$$ LANGUAGE plpgsql;
""").format(minvalue=1, maxvalue=((2 ** 32) - 1))

sequence_drop = (
    "DROP SEQUENCE IF EXISTS maasserver_zone_serial_seq"
)


class Migration(migrations.Migration):

    dependencies = [
        ('maasserver', '0021_create_node_system_id_sequence'),
    ]

    operations = [
        migrations.RunSQL(sequence_create, sequence_drop),
    ]
