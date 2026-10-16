from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('care_emergency', '0011_patient_merge'),
    ]

    operations = [
        migrations.AddField(
            model_name='patientregistration',
            name='international_tariff_consent',
            field=models.BooleanField(default=False),
        ),
    ]
