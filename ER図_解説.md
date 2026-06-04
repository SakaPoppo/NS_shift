テーブル詳細

users
id : bigint / ユーザーID
username : string / ログイン用ユーザー名（ユニーク制約）
email : string / メールアドレス
password : string / ハッシュ化されたパスワード
is_active : boolean / ユーザーが有効状態かどうか
date_joined : datetime / ユーザー登録日時

staff_members
id : bigint / スタッフID
user_id : bigint / スタッフを登録・管理しているユーザーID
name : string / スタッフ名
job : string / 職能・職種（看護師、准看護師、介護士など）
role : string / シフト上の役割（リーダー、メンバーなど）
can_night_shift : boolean / 夜勤が可能かどうか
is_active : boolean / 現在もシフト作成対象かどうか
created_at : datetime / 作成日時
updated_at : datetime / 更新日時

staff_regular_days_off
id : bigint / 固定休曜日ID
staff_member_id : bigint / 対象スタッフID
day_of_week : integer / 固定休の曜日（0=月、1=火、2=水、3=木、4=金、5=土、6=日）
created_at : datetime / 作成日時
updated_at : datetime / 更新日時

shift_plans
id : bigint / シフト表ID
user_id : bigint / シフト表を作成したユーザーID
year : integer / シフト表の年
month : integer / シフト表の月
title : string / シフト表のタイトル
status : string / シフト表の状態（draft、generated、confirmed）
created_at : datetime / 作成日時
updated_at : datetime / 更新日時

shift_rules
id : bigint / シフト作成ルールID
shift_plan_id : bigint / 対象のシフト表ID
off_days_per_staff : integer / スタッフ1人あたりの月の休み数
max_consecutive_work_days : integer / 最大連勤数
night_shift_next_day_off : boolean / 夜勤明けの翌日を休みにするかどうか
required_day_staff : integer / 1日あたりの必要日勤人数
required_night_staff : integer / 1日あたりの必要夜勤人数
created_at : datetime / 作成日時
updated_at : datetime / 更新日時

day_off_requests
id : bigint / 希望休ID
shift_plan_id : bigint / 対象のシフト表ID
staff_member_id : bigint / 希望休を出したスタッフID
date : date / 希望休の日付
memo : text / 補足メモ
created_at : datetime / 作成日時
updated_at : datetime / 更新日時

shift_results
id : bigint / 勤務割り当てID
shift_plan_id : bigint / 対象のシフト表ID
staff_member_id : bigint / 対象スタッフID
date : date / 勤務日
shift_type : string / 勤務種別（day、night、after_night、off）
input_type : string / 入力方法（manual、generated）
is_locked : boolean / 自動生成時に上書きしないかどうか
memo : text / 補足メモ
created_at : datetime / 作成日時
updated_at : datetime / 更新日時