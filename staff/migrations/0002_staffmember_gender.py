from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("staff", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="staffmember",
            name="gender",
            field=models.CharField(
                blank=True,
                choices=[("male", "男性"), ("female", "女性")],
                max_length=10,
                verbose_name="性別",
            ),
        ),
    ]
