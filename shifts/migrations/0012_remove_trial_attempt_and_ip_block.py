from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("shifts", "0011_trialipblock_trialgenerationattempt_and_more"),
    ]

    operations = [
        migrations.DeleteModel(name="TrialGenerationAttempt"),
        migrations.DeleteModel(name="TrialIPBlock"),
    ]
