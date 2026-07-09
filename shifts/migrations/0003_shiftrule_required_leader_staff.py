from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("shifts", "0002_shiftrule_dayoffrequest_shiftresult"),
    ]

    operations = [
        migrations.AddField(
            model_name="shiftrule",
            name="required_leader_staff",
            field=models.IntegerField(default=0, verbose_name="各勤務に必要なリーダークラス"),
        ),
    ]
