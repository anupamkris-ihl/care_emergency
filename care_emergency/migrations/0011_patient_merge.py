import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('care_emergency', '0010_emergency_complaint_arrival'),
        ('emr', '0083_backfill_approximate_date_of_birth'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PatientMerge',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('external_id', models.UUIDField(db_index=True, default=uuid.uuid4, unique=True)),
                ('created_date', models.DateTimeField(auto_now_add=True, db_index=True, null=True)),
                ('modified_date', models.DateTimeField(auto_now=True, db_index=True, null=True)),
                ('deleted', models.BooleanField(db_index=True, default=False)),
                ('history', models.JSONField(default=dict)),
                ('meta', models.JSONField(default=dict)),
                ('reason', models.CharField(max_length=500)),
                ('choices', models.JSONField(default=dict)),
                ('source_snapshot', models.JSONField(default=dict)),
                ('moved', models.JSONField(default=dict)),
                ('created_by', models.ForeignKey(blank=True, default=None, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(app_label)s_%(class)s_created_by', to=settings.AUTH_USER_MODEL)),
                ('source', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='emr.patient')),
                ('target', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='merged_from', to='emr.patient')),
                ('updated_by', models.ForeignKey(blank=True, default=None, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(app_label)s_%(class)s_updated_by', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
