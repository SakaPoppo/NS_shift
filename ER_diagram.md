## テーブル詳細

### users

Django標準のユーザー認証機能を想定したテーブルです。
実装時はDjango標準Userを使用する想定であり、実テーブル名は `auth_user` になります。

* id : bigint / ユーザーID
* username : string / ログイン用ユーザー名
* email : string / メールアドレス
* password: string / ハッシュ化されたパスワード
* is_active : boolean / ユーザーが有効状態かどうか
* date_joined : datetime / ユーザー登録日時

#### 制約

* username はユニーク制約を設定します。
* パスワードは平文では保存せず、Django標準の認証機能によりハッシュ化された値を保存します。

---

### staff_members

シフト作成の対象となるスタッフを管理するテーブルです。
1人のユーザーが複数のスタッフを登録・管理できます。

* id : bigint / スタッフID
* user_id : bigint / スタッフを登録・管理しているユーザーID
* name : string / スタッフ名
* job : string / 職能・職種（看護師、准看護師、介護士など）
* role : string / シフト上の役割（leader、member など）
* can_night_shift : boolean / 夜勤が可能かどうか
* is_active : boolean / 現在もシフト作成対象かどうか
* created_at : datetime / 作成日時
* updated_at : datetime / 更新日時

#### 制約

* user_id は users.id を参照します。
* is_active を false にすることで、退職者や異動者を過去データとして残しつつ、今後のシフト作成対象から外せるようにします。

---

### staff_regular_days_off

スタッフごとの固定休曜日を管理するテーブルです。
例として、毎週月曜日が固定休のスタッフなどを登録します。

* id : bigint / 固定休曜日ID
* staff_member_id : bigint / 対象スタッフID
* day_of_week : integer / 固定休の曜日（0=月、1=火、2=水、3=木、4=金、5=土、6=日）
* created_at : datetime / 作成日時
* updated_at : datetime / 更新日時

#### 制約

* staff_member_id は staff_members.id を参照します。
* 同じスタッフに同じ曜日の固定休が重複して登録されないように、以下の組み合わせにユニーク制約を設定します。

```txt
staff_member_id + day_of_week
```

---

### shift_plans

月単位のシフト表を管理するテーブルです。
1つの shift_plan が「2026年7月分のシフト表」のような1ヶ月分のシフト表を表します。

* id : bigint / シフト表ID
* user_id : bigint / シフト表を作成したユーザーID
* year : integer / シフト表の年
* month : integer / シフト表の月
* title : string / シフト表のタイトル
* status : string / シフト表の状態（draft、generated、confirmed）
* created_at : datetime / 作成日時
* updated_at : datetime / 更新日時

#### 制約

* user_id は users.id を参照します。
* 同じユーザーが同じ年月のシフト表を重複して作成しないように、以下の組み合わせにユニーク制約を設定します。

```txt
user_id + year + month
```

---

### shift_rules

シフト自動作成時に使用するルールを管理するテーブルです。
1つの shift_plan に対して、1つの shift_rules を紐づけます。

* id : bigint / シフト作成ルールID
* shift_plan_id : bigint / 対象のシフト表ID
* off_days_per_staff : integer / スタッフ1人あたりの月の休み数
* max_consecutive_work_days : integer / 最大連勤数
* night_shift_next_day_off : boolean / 夜勤明け翌日に勤務を入れないかどうか
* required_day_staff : integer / 1日あたりの必要日勤人数
* required_night_staff : integer / 1日あたりの必要夜勤人数
* created_at : datetime / 作成日時
* updated_at : datetime / 更新日時

#### 制約

* shift_plan_id は shift_plans.id を参照します。
* 1つのシフト表に対してルールが複数作成されないように、shift_plan_id にユニーク制約を設定します。

```txt
shift_plan_id
```

#### 夜勤明けの扱い

`night_shift_next_day_off` は、夜勤翌日に通常勤務を入れないためのルールです。
night => after_nightは固定であり、翌日がoffもしくはoff_requestになるか、それ以外かを規定します

---

### day_off_requests

スタッフごとの希望休を管理するテーブルです。
このテーブルは「希望休の申請データ」を表します。

* id : bigint / 希望休ID
* shift_plan_id : bigint / 対象のシフト表ID
* staff_member_id : bigint / 希望休を出したスタッフID
* date : date / 希望休の日付
* memo : text / 補足メモ
* created_at : datetime / 作成日時
* updated_at : datetime / 更新日時

#### 制約

* shift_plan_id は shift_plans.id を参照します。
* staff_member_id は staff_members.id を参照します。
* 同じスタッフが、同じシフト表・同じ日付に重複して希望休を登録できないように、以下の組み合わせにユニーク制約を設定します。

```txt
shift_plan_id + staff_member_id + date
```

---

### shift_results

自動生成または手動入力された勤務割り当て結果を管理するテーブルです。
1レコードが「1つのシフト表における、1人のスタッフの1日分の勤務」を表します。

* id : bigint / 勤務割り当てID
* shift_plan_id : bigint / 対象のシフト表ID
* staff_member_id : bigint / 対象スタッフID
* date : date / 勤務日
* shift_type : string / 勤務種別（day、night、after_night、off、off_request）
* input_type : string / 入力方法（manual、generated）
* is_locked : boolean / 自動生成時に上書きしないかどうか
* memo : text / 補足メモ
* created_at : datetime / 作成日時
* updated_at : datetime / 更新日時

#### shift_type の種類

* day : 日勤
* night : 夜勤
* after_night : 夜勤明け
* off : 通常の休み
* off_request : 希望休として反映された休み

`day_off_requests` は希望休の申請データを管理し、`shift_results.off_request` はその希望休がシフト結果に反映された状態を表します。

#### 制約

* shift_plan_id は shift_plans.id を参照します。
* staff_member_id は staff_members.id を参照します。
* 同じシフト表内で、同じスタッフの同じ日付に複数の勤務結果が登録されないように、以下の組み合わせにユニーク制約を設定します。

```txt
shift_plan_id + staff_member_id + date
```

この制約により、同じスタッフに同じ日付で「日勤」と「夜勤」が同時に登録されるような重複を防ぎます。

なお、shift_type はユニーク制約には含めません。
shift_type を含めてしまうと、同じスタッフ・同じ日付でも shift_type が異なれば別レコードとして登録できてしまい、重複勤務を防げなくなるためです。
