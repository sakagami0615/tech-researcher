# 要件

仕様の正は次の文書（Why・What）とし、本ファイルはその入口とする。

- [concept.md](concept.md)：コンセプト（Why）
- [epics.md](epics.md)：エピック（What - 大）
- [user-stories.md](user-stories.md)：ユーザストーリー（What - 小）

実現方式（How）の決定事項は [decisions.md](decisions.md) に記録する。

決定事項にもとづく詳細設計（具体的な形・中身）は [design/](design/) に置く。

- [design/report-templates.md](design/report-templates.md)：レポート Issue・不合格 Issue・実行エラー Issue のテンプレートと重複判定の手順
- [design/data-model.md](design/data-model.md)：SQLite のテーブル、共通収集データのスキーマ、AI の入力と出力の JSON
- [design/structure.md](design/structure.md)：ディレクトリ構成、起動の構成、設定ファイル、期間の扱い、再試行と上限、ログ・ドライラン、実装の技術・依存・テスト
