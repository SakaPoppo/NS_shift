# コントリビューションガイド

このドキュメントでは、Issueの作成からPull Request（PR）のマージまでの基本的な開発ルールを定めます。

## 開発フロー

基本的に、以下の手順で開発します。

1. Issueを作成して作業内容を整理する
2. 作業用ブランチを作成する
3. 実装・修正を行う
4. 必要なテストと動作確認を実施する
5. PRを作成し、関連Issueを紐付ける
6. CIの成功を確認してマージする

## Issue命名規則

Issueのタイトルには、内容に応じて以下の形式を使用します。

- 機能追加・改善：`[Feature] 内容`
- バグ修正：`[Bug] 内容`

Issueの本文には、`.github/ISSUE_TEMPLATE/` に用意されたテンプレートを使用します。

## ブランチ命名規則

ブランチ名は、以下の形式に統一します。

```text
type/description
```

| type | 用途 |
| --- | --- |
| `feature` | 新機能の追加 |
| `fix` | バグ修正 |
| `refactor` | リファクタリング |
| `chore` | 設定変更・開発環境の整備 |
| `docs` | ドキュメント更新 |
| `test` | テストの追加・修正 |
| `perf` | パフォーマンス改善 |
| `ci` | CI/CD関連の変更 |

命名時は、以下のルールに従います。

- 英語の小文字を使用する
- 単語はハイフン（`-`）で区切る
- 作業内容がわかる簡潔な名称にする
- Issue番号は必要に応じて付ける

例：

- `feature/user-events`
- `fix/night-shift-count`
- `refactor/shift-generator`
- `chore/add-github-templates`
- `test/shift-constraints`
- `feature/123-user-events`

## PRタイトル命名規則

PRタイトルにはConventional Commits形式を使用します。

```text
type: 変更内容
```

| type | 用途 |
| --- | --- |
| `feat` | 新機能の追加 |
| `fix` | バグ修正 |
| `refactor` | リファクタリング |
| `chore` | 設定変更・開発環境の整備 |
| `docs` | ドキュメント更新 |
| `test` | テストの追加・修正 |
| `perf` | パフォーマンス改善 |
| `ci` | CI/CD関連の変更 |

例：

- `feat: ユーザーイベントログを追加`
- `fix: 夜勤回数の集計不具合を修正`
- `refactor: シフト生成処理を整理`
- `chore: GitHubテンプレートを追加`

PRの本文には、`.github/pull_request_template.md` を使用します。関連Issueがある場合は、`Closes #123` の形式で紐付けます。

## コミットメッセージ

コミットメッセージにもConventional Commits形式を使用します。

```text
type: 変更内容
```

typeはPRタイトルと同じものを使用し、変更内容がわかる簡潔な日本語で記載します。

例：

- `feat: イベントログ保存処理を追加`
- `fix: 日勤人数の計算処理を修正`
- `test: シフト生成APIのテストを追加`

## PR作成時の確認事項

- PRの目的と変更内容が明確であること
- 関連Issueがあれば紐付けること
- 変更箇所に応じた動作確認を実施すること
- 必要に応じてテストを追加・修正すること
- CIが設定されている場合は成功を確認すること
- DB変更、API仕様変更、環境変数の追加などがある場合はPRに記載すること
