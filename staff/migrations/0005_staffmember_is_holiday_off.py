from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("staff", "0004_staffmember_ability_level_alter_staffmember_job")]

    operations = [
        migrations.AddField(
            model_name="staffmember",
            name="is_holiday_off",
            field=models.BooleanField(default=False, verbose_name="祝日固定休"),
        ),
    ]
