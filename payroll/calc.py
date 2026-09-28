"""月次の報酬計算ロジック（純粋関数・DB非依存）。

人間は事実（出勤日数・件数・各金額）を入力するだけで、計算はすべてここで行う。
"""
from dataclasses import dataclass, field, asdict

from . import rates

CATEGORIES = {
    "contractor": "個人事業主",
    "employee": "正社員",
    "intern": "インターン",
}

# 支払項目の種類。sign=+1 は支給、-1 は控除。
ITEM_TYPES = {
    "transport": {"label": "交通費", "sign": 1},
    "advance": {"label": "立替金", "sign": 1},
    "expense": {"label": "立替・経費（会社負担）", "sign": 1},
    "other_reward": {"label": "その他報酬", "sign": 1},
    "contract_reward": {"label": "契約報酬", "sign": 1},
    "gasoline": {"label": "ガソリン代", "sign": -1},
}

# 支払一覧の「その他」列に集計する項目
OTHER_TYPES = ("other_reward", "contract_reward")

CAR_ALLOWANCE = 20000  # 正社員が自家用車を使用する場合のみ（個人事業主は対象外）


@dataclass
class Issue:
    level: str  # "error"（計算不可・要入力）/ "warning"（要確認）
    message: str


@dataclass
class Result:
    category: str
    work_days: object = None
    acquisitions: object = None
    prev_count: object = None
    prev_count_source: str = ""  # "auto" / "manual" / ""
    rate: object = None
    sales_reward: object = None
    car_allowance: int = 0
    totals: dict = field(default_factory=dict)  # item_type -> 合計
    other_total: int = 0
    payment_total: object = None
    issues: list = field(default_factory=list)

    @property
    def errors(self):
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self):
        return [i for i in self.issues if i.level == "warning"]

    def to_dict(self):
        return asdict(self)


def compute(category, work_days, acquisitions, prev_count, prev_count_source,
            items, days_in_month=31, expense_cap=None, uses_private_car=False,
            prev_missing_message="前月獲得件数を取得できません（前月データがありません）"):
    """1名・1か月分を計算する。

    prev_count   : 前月獲得件数（単価判定専用）。取得できない場合は None。
    acquisitions : 当月獲得件数（報酬計算専用）。
    items        : [{"item_type", "amount", ...}, ...] 当月支払分の内訳。
    """
    r = Result(category=category, work_days=work_days, acquisitions=acquisitions,
               prev_count=prev_count, prev_count_source=prev_count_source)

    totals = {t: 0 for t in ITEM_TYPES}
    for it in items:
        totals[it["item_type"]] += int(it["amount"])
    r.totals = totals
    r.other_total = sum(totals[t] for t in OTHER_TYPES)

    for it in items:
        if it["item_type"] == "gasoline" and not it.get("target_month"):
            r.issues.append(Issue("error", "ガソリン代の「対象月（何月分か）」が未入力です"))
            break

    if category != "contractor":
        if category == "employee" and uses_private_car:
            r.car_allowance = CAR_ALLOWANCE
        r.issues.append(Issue(
            "warning",
            f"{CATEGORIES.get(category, category)}の給与計算ルールは未定義のため、"
            "支払総額を自動計算していません（仕様確認が必要）"))
        return r

    # ---- 個人事業主 ----
    before = len(r.issues)
    if work_days is None:
        r.issues.append(Issue("error", "当月出勤日数が未入力です"))
    elif not 0 <= work_days <= min(days_in_month, rates.MAX_WORK_DAYS):
        r.issues.append(Issue("error", f"出勤日数は0～{days_in_month}日で入力してください"))
    if acquisitions is None:
        r.issues.append(Issue("error", "当月獲得件数が未入力です"))
    elif acquisitions < 0:
        r.issues.append(Issue("error", "当月獲得件数が不正です"))
    if prev_count is None:
        r.issues.append(Issue("error", prev_missing_message))

    if len(r.issues) == before:
        # 単価 = 前月件数 × 当月出勤日数 で判定
        r.rate = rates.lookup_rate(prev_count, work_days)
        # 営業報酬 = 当月件数 × 単価（前月件数ではない）
        r.sales_reward = acquisitions * r.rate

    if expense_cap is not None and totals["expense"] > expense_cap:
        r.issues.append(Issue(
            "warning",
            f"立替・経費が目安（{expense_cap:,}円）を超えています：{totals['expense']:,}円"))

    if r.sales_reward is not None and not r.errors:
        r.payment_total = (
            r.sales_reward
            + totals["transport"]
            + totals["advance"]
            + totals["expense"]
            + r.other_total
            - totals["gasoline"]
        )
        if r.payment_total < 0:
            r.issues.append(Issue("warning", "支払総額がマイナスです"))
    return r
