import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('care_emergency', '0012_international_tariff_consent'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='NewbornBirth',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('external_id', models.UUIDField(db_index=True, default=uuid.uuid4, unique=True)),
                ('created_date', models.DateTimeField(auto_now_add=True, db_index=True, null=True)),
                ('modified_date', models.DateTimeField(auto_now=True, db_index=True, null=True)),
                ('deleted', models.BooleanField(db_index=True, default=False)),
                ('history', models.JSONField(default=dict)),
                ('meta', models.JSONField(default=dict)),
                ('born_at', models.DateTimeField()),
                ('birth_order', models.PositiveSmallIntegerField(default=1)),
                ('number_of_babies', models.PositiveSmallIntegerField(default=1)),
                ('delivery_type', models.CharField(max_length=30)),
                ('gestation_weeks', models.PositiveSmallIntegerField()),
                ('birth_weight_grams', models.PositiveSmallIntegerField()),
                ('length_cm', models.DecimalField(blank=True, decimal_places=1, max_digits=4, null=True)),
                ('head_circumference_cm', models.DecimalField(blank=True, decimal_places=1, max_digits=4, null=True)),
                ('apgar_1_min', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('apgar_5_min', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('baby', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='newborn_birth', to='emr.patient')),
                ('created_by', models.ForeignKey(blank=True, default=None, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(app_label)s_%(class)s_created_by', to=settings.AUTH_USER_MODEL)),
                ('facility', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='facility.facility')),
                ('mother', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='newborn_babies', to='emr.patient')),
                ('updated_by', models.ForeignKey(blank=True, default=None, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='%(app_label)s_%(class)s_updated_by', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
