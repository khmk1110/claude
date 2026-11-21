# 社内管理システム

シフト・勤怠・有給・メンバー・給与報酬・資料経費を一元管理する Web システム（Django + PostgreSQL）。

- 設計: [docs/02_全体設計.md](docs/02_全体設計.md)
- **実データ（氏名・給与・カード番号・元資料）はリポジトリに入れないこと。**

## 開発環境

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env              # DATABASE_URL などを設定
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
# 給与用・経費用の追加パスワードを設定（ハッシュ化して DB に保存）
.venv/bin/python manage.py set_stepup_password --scope payroll
.venv/bin/python manage.py set_stepup_password --scope expenses
.venv/bin/python manage.py runserver
```

## テスト

```bash
.venv/bin/python manage.py test
```
